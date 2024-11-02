use crate::ffi::SamplingParams;
use text_generation_router::infer::{InferError, InferStreamResponse};
use tokio::sync::mpsc::UnboundedSender;

pub mod backend;

impl Default for SamplingParams {
    fn default() -> Self {
        Self {
            top_k: u32::MAX,
            top_p: 1.0f32,
            frequency_penalty: 0.0f32,
            repetition_penalty: 0.0f32,
            seed: 2014u64,
        }
    }
}

struct OpaqueStream(UnboundedSender<Result<InferStreamResponse, InferError>>);

#[cxx::bridge(namespace = "huggingface::tgi::backends::llamacpp")]
mod ffi {
    #[derive(Debug, Copy, Clone)]
    struct GenerationParams {
        max_new_tokens: u32,
        ignore_eos_token: bool,
    }

    #[derive(Debug, Copy, Clone)]
    struct SamplingParams {
        top_k: u32,
        top_p: f32,
        frequency_penalty: f32,
        repetition_penalty: f32,
        seed: u64,
    }

    extern "Rust" {
        type OpaqueStream;
    }

    unsafe extern "C++" {
        include!("backends/llamacpp/csrc/ffi.hpp");

        #[cxx_name = "generation_params_t"]
        type GenerationParams;

        #[cxx_name = "sampling_params_t"]
        type SamplingParams;

        /// Represent an instance of the llama.cpp backend instance on C++ side
        #[cxx_name = "llama_cpp_backend_impl_t"]
        type LlamaCppBackendImpl;

        #[rust_name = "create_single_worker_backend"]
        fn create_single_worker_backend(modelPath: &str) -> Result<UniquePtr<LlamaCppBackendImpl>>;

        // fn generate(
        //     self: Pin<&mut LlamaCppBackendImpl>,
        //     tokens: &[u32],
        //     generated: &mut [u32],
        //     generation_params: GenerationParams,
        //     sampling_params: &SamplingParams,
        // ) -> Result<usize>;

        unsafe fn stream(
            self: Pin<&mut LlamaCppBackendImpl>,
            tokens: &[u32],
            generated: &mut [u32],
            generation_params: GenerationParams,
            sampling_params: &SamplingParams,
            stream: *mut OpaqueStream,
            callback: unsafe fn(*mut OpaqueStream, u32, f32, bool),
        ) -> Result<usize>;
    }
}