set(USE_CXX11_ABI ON)
set(NVTX_DISABLE OFF)
set(BUILD_PYT OFF)
set(BUILD_PYBIND OFF)
set(BUILD_MICRO_BENCHMARKS OFF)
set(BUILD_BENCHMARKS OFF)
set(BUILD_TESTS OFF)
set(TRT_INCLUDE_DIR ${TGI_TRTLLM_BACKEND_TRT_INCLUDE_DIR})
set(TRT_LIB_DIR ${TGI_TRTLLM_BACKEND_TRT_LIB_DIR})
set(CMAKE_CUDA_ARCHITECTURES ${TGI_TRTLLM_BACKEND_TARGET_CUDA_ARCH_LIST})

#if (NOT EXISTS ${TGI_TRTLLM_BACKEND_TRT_ROOT})
#    message(FATAL_ERROR "TensorRT specified location: ${TGI_TRTLLM_BACKEND_TRT_ROOT} doesn't exist")
#else ()
#    if (NOT EXISTS ${TGI_TRTLLM_BACKEND_TRT_INCLUDE_DIR})
#        message(FATAL_ERROR "TensorRT headers were not found at: ${TGI_TRTLLM_BACKEND_TRT_INCLUDE_DIR}")
#    endif ()
#
#    if (NOT EXISTS ${TGI_TRTLLM_BACKEND_TRT_LIB_DIR})
#        message(FATAL_ERROR "TensorRT libraries were not found at: ${TGI_TRTLLM_BACKEND_TRT_LIB_DIR}")
#    endif ()
#endif ()

message(STATUS "Building for CUDA Architectures: ${CMAKE_CUDA_ARCHITECTURES}")

if (${CMAKE_BUILD_TYPE} STREQUAL "Debug")
    set(FAST_BUILD ON)
else ()
    set(FAST_BUILD OFF)
endif ()

fetchcontent_declare(
        trtllm
        GIT_REPOSITORY https://github.com/nvidia/tensorrt-llm.git
        GIT_TAG 9691e12bce7ae1c126c435a049eb516eb119486c
        GIT_SHALLOW TRUE
)
fetchcontent_makeavailable(trtllm)
message(STATUS "Found TensorRT-LLM: ${trtllm_SOURCE_DIR}")
execute_process(COMMAND git lfs install WORKING_DIRECTORY "${trtllm_SOURCE_DIR}/")
execute_process(COMMAND git lfs pull WORKING_DIRECTORY "${trtllm_SOURCE_DIR}/")
add_subdirectory("${trtllm_SOURCE_DIR}/cpp")
include_directories("${trtllm_SOURCE_DIR}/cpp/include")
