# allow using other images to build kernels
ARG KERNEL_BUILDER_IMAGE=kernel-builder
# Allow using other images as pytorch base image
ARG PYTORCH_IMAGE=pytorch-install

# Rust builder
FROM lukemathwalker/cargo-chef:latest-rust-1.67 AS chef
WORKDIR /usr/src

FROM chef as planner
COPY Cargo.toml Cargo.toml
COPY rust-toolchain.toml rust-toolchain.toml
COPY proto proto
COPY router router
COPY launcher launcher
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder

RUN PROTOC_ZIP=protoc-21.12-linux-x86_64.zip && \
    curl -OL https://github.com/protocolbuffers/protobuf/releases/download/v21.12/$PROTOC_ZIP && \
    unzip -o $PROTOC_ZIP -d /usr/local bin/protoc && \
    unzip -o $PROTOC_ZIP -d /usr/local 'include/*' && \
    rm -f $PROTOC_ZIP

COPY --from=planner /usr/src/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json

COPY Cargo.toml Cargo.toml
COPY rust-toolchain.toml rust-toolchain.toml
COPY proto proto
COPY router router
COPY launcher launcher
RUN cargo build --release

# Python builder
# Adapted from: https://github.com/pytorch/pytorch/blob/master/Dockerfile
FROM ubuntu:22.04 as pytorch-install

ARG PYTORCH_VERSION=2.0.0
ARG PYTHON_VERSION=3.9
ARG CUDA_VERSION=11.8
ARG CUDA_CHANNEL=nvidia
ARG INSTALL_CHANNEL=pytorch
# Automatically set by buildx
ARG TARGETPLATFORM

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        curl \
        git && \
        rm -rf /var/lib/apt/lists/*
RUN /usr/sbin/update-ccache-symlinks &&  \
    mkdir /opt/ccache &&  \
    ccache --set-config=cache_dir=/opt/ccache
ENV PATH /opt/conda/bin:$PATH

# Install conda
# translating Docker's TARGETPLATFORM into miniconda arches
RUN case ${TARGETPLATFORM} in \
         "linux/arm64")  MINICONDA_ARCH=aarch64  ;; \
         *)              MINICONDA_ARCH=x86_64   ;; \
    esac && \
    curl -fsSL -v -o ~/miniconda.sh -O  "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${MINICONDA_ARCH}.sh"
# Manually invoke bash on miniconda script per https://github.com/conda/conda/issues/10431
RUN chmod +x ~/miniconda.sh && \
    bash ~/miniconda.sh -b -p /opt/conda && \
    rm ~/miniconda.sh

# Install pytorch
# On arm64 we exit with an error code
RUN case ${TARGETPLATFORM} in \
         "linux/arm64")  exit 1 ;; \
         *)              /opt/conda/bin/conda update -y conda &&  \
                         /opt/conda/bin/conda install -c "${INSTALL_CHANNEL}" -c "${CUDA_CHANNEL}" -y "python=${PYTHON_VERSION}" pytorch==$PYTORCH_VERSION "pytorch-cuda=$(echo $CUDA_VERSION | cut -d'.' -f 1-2)"  ;; \
    esac && \
    /opt/conda/bin/conda clean -ya

# CUDA kernels builder image
FROM $PYTORCH_IMAGE as kernel-builder

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ninja-build \
        && rm -rf /var/lib/apt/lists/*

RUN /opt/conda/bin/conda install -c "nvidia/label/cuda-11.8.0"  cuda==11.8 && \
    /opt/conda/bin/conda clean -ya


# Build Flash Attention CUDA kernels
FROM $KERNEL_BUILDER_IMAGE as flash-att-builder

WORKDIR /usr/src

COPY server/Makefile-flash-att Makefile

# Build specific version of flash attention
RUN make build-flash-attention

# Build Transformers CUDA kernels
FROM $KERNEL_BUILDER_IMAGE as transformers-builder

WORKDIR /usr/src

COPY server/Makefile-transformers Makefile

# Build specific version of transformers
RUN BUILD_EXTENSIONS="True" make build-transformers

# re-export because `COPY --from` does not support ARG vars directly
FROM $PYTORCH_IMAGE as pytorch

# Text Generation Inference base image
FROM ubuntu:22.04 as base

ARG TARGETPLATFORM
ARG PYTORCH_VERSION=2.0.0
ARG CUDA_VERSION=11.8

# Conda and CUDA env
ENV PATH=/opt/conda/bin:$PATH \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64

# Text Generation Inference base env
ENV HUGGINGFACE_HUB_CACHE=/data \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    MODEL_ID=bigscience/bloom-560m \
    QUANTIZE=false \
    NUM_SHARD=1 \
    PORT=80

LABEL com.nvidia.volumes.needed="nvidia_driver"

WORKDIR /usr/src

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libssl-dev \
        make \
        && rm -rf /var/lib/apt/lists/*

# Copy conda with PyTorch installed
COPY --from=pytorch /opt/conda /opt/conda

# Copy build artifacts from flash attention builder
COPY --from=flash-att-builder /usr/src/flash-attention/build/lib.linux-x86_64-cpython-39 /opt/conda/lib/python3.9/site-packages
COPY --from=flash-att-builder /usr/src/flash-attention/csrc/layer_norm/build/lib.linux-x86_64-cpython-39 /opt/conda/lib/python3.9/site-packages
COPY --from=flash-att-builder /usr/src/flash-attention/csrc/rotary/build/lib.linux-x86_64-cpython-39 /opt/conda/lib/python3.9/site-packages

# Copy build artifacts from transformers builder
COPY --from=transformers-builder /usr/src/transformers /usr/src/transformers
COPY --from=transformers-builder /usr/src/transformers/build/lib.linux-x86_64-cpython-39/transformers /usr/src/transformers/src/transformers

# Install transformers dependencies
RUN cd /usr/src/transformers && pip install -e . --no-cache-dir

# Install server
COPY proto proto
COPY server server
COPY server/Makefile server/Makefile
RUN cd server && \
    make gen-server && \
    pip install ".[bnb]" --no-cache-dir

# Install router
COPY --from=builder /usr/src/target/release/text-generation-router /usr/local/bin/text-generation-router
# Install launcher
COPY --from=builder /usr/src/target/release/text-generation-launcher /usr/local/bin/text-generation-launcher

# AWS Sagemaker compatbile image
FROM base as sagemaker

COPY sagemaker-entrypoint.sh entrypoint.sh
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]

# Final image
FROM base

ENTRYPOINT ["text-generation-launcher"]
CMD ["--json-output"]