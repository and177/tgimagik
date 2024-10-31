use crate::ffi::{
    create_single_worker_backend, GenerationParams, LlamaCppBackendImpl, SamplingParams,
};
use async_trait::async_trait;
use cxx::{Exception, UniquePtr};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread::spawn;
use text_generation_router::infer::{Backend, InferError, InferStreamResponse};
use text_generation_router::validation::ValidGenerateRequest;
use thiserror::Error;
use tokio_stream::wrappers::UnboundedReceiverStream;
use tracing::info;

unsafe impl Send for LlamaCppBackendImpl {}

#[derive(Debug, Error)]
pub enum LlamaCppBackendError {
    #[error("Provided GGUF model path {0} doesn't exist")]
    ModelFileDoesntExist(String),

    #[error("Failed to initialize model from GGUF file {0}: {1}")]
    ModelInitializationFailed(PathBuf, String),
}

pub struct LlamaCppBackend {}

impl LlamaCppBackend {
    pub fn new<P: AsRef<Path> + Send>(model_path: P) -> Result<Self, LlamaCppBackendError> {
        let path = Arc::new(model_path.as_ref());
        if !path.exists() {
            return Err(LlamaCppBackendError::ModelFileDoesntExist(
                path.display().to_string(),
            ));
        }

        let mut backend = create_single_worker_backend(path.to_str().unwrap()).map_err(|err| {
            LlamaCppBackendError::ModelInitializationFailed(
                path.to_path_buf(),
                err.what().to_string(),
            )
        })?;

        info!(
            "Successfully initialized llama.cpp backend from {}",
            path.display()
        );

        let j = spawn(|| scheduler_loop(backend));
        j.join().ok();
        Ok(Self {})
    }
}

fn scheduler_loop(mut backend: UniquePtr<LlamaCppBackendImpl>) {
    println!("Scheduler loop");
    let tokens = [128000u32, 5159, 836, 374, 23809];
    let mut generated = vec![0u32; 16];
    let generation_params = GenerationParams {
        max_new_tokens: generated.len() as u32,
    };
    let sampling_params = SamplingParams::default();

    match backend.pin_mut().generate(
        &tokens,
        &mut generated,
        &generation_params,
        &sampling_params,
        |new_token_id: u32, is_eos: bool| println!("Generated {new_token_id} (is_eos: {is_eos})"),
    ) {
        Ok(n_tokens) => {
            generated.truncate(n_tokens);
            println!("Generated {} tokens -> {:?}", n_tokens, generated);
        }
        Err(err) => println!("Error: {}", err),
    }
}

#[async_trait]
impl Backend for LlamaCppBackend {
    fn schedule(
        &self,
        _request: ValidGenerateRequest,
    ) -> Result<UnboundedReceiverStream<Result<InferStreamResponse, InferError>>, InferError> {
        Err(InferError::GenerationError("Not implemented yet".into()))
    }

    async fn health(&self, _: bool) -> bool {
        true
    }
}
