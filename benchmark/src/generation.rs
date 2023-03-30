use std::time::{Duration, Instant};
use text_generation_client::{Batch, ClientError, NextTokenChooserParameters, Request, ShardedClient, StoppingCriteriaParameters};
use tokenizers::{Tokenizer, TruncationDirection};
use tokio::sync::{broadcast, mpsc};

const LOREM_IPSUM: &str = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.";

#[derive(Debug, Clone)]
pub(crate) struct Prefill {
    pub(crate) latency: Duration,
    pub(crate) throughput: f64,
}

#[derive(Debug, Clone)]
pub(crate) struct Decode {
    pub(crate) decode_length: u32,
    pub(crate) latency: Duration,
    pub(crate) throughput: f64,
}

#[derive(Debug)]
pub(crate) struct Run {
    pub(crate) batch_size: u32,
    pub(crate) sequence_length: u32,
    pub(crate) prefill: Prefill,
    pub(crate) decode: Decode,
}

#[derive(Debug)]
pub(crate) enum Message {
    Warmup,
    Prefill(Prefill),
    Decode(Decode),
    Run(Run),
    EndBatch,
}

pub(crate) async fn generation_task(
    tokenizer: Tokenizer,
    batch_size: Vec<u32>,
    sequence_length: u32,
    decode_length: u32,
    n_runs: usize,
    warmups: usize,
    client: ShardedClient,
    run_sender: mpsc::Sender<Result<Message, ClientError>>,
    mut shutdown_receiver: broadcast::Receiver<()>,
    _shutdown_guard_sender: mpsc::Sender<()>,
) {
    tokio::select! {
        res = generate_runs(tokenizer, batch_size, sequence_length, decode_length, n_runs, warmups, client, run_sender.clone())  => {
            if let Err(err) = res {
                run_sender.send(Err(err)).await.unwrap_or(());
            }
        },
        _ = shutdown_receiver.recv() => {}
    }
}

async fn generate_runs(tokenizer: Tokenizer,
                       batch_size: Vec<u32>,
                       sequence_length: u32,
                       decode_length: u32,
                       n_runs: usize,
                       warmups: usize,
                       mut client: ShardedClient,
                       run_sender: mpsc::Sender<Result<Message, ClientError>>,
) -> Result<(), ClientError> {
    let sequence = create_sequence(sequence_length, tokenizer);

    for b in batch_size {
        for _ in 0..warmups {
            let (_, decode_batch) = prefill(sequence.clone(), b, decode_length, &mut client).await?;
            let _ = decode(decode_batch, &mut client).await?;
            run_sender.send(Ok(Message::Warmup)).await.unwrap_or(());
        }

        for _ in 0..n_runs {
            let (prefill, decode_batch) = prefill(sequence.clone(), b, decode_length, &mut client).await?;
            run_sender
                .send(Ok(Message::Prefill(prefill.clone())))
                .await
                .unwrap_or(());

            let decode = decode(decode_batch, &mut client).await?;

            run_sender
                .send(Ok(Message::Decode(decode.clone())))
                .await
                .unwrap_or(());

            run_sender.send(Ok(Message::Run(Run {
                batch_size: b,
                sequence_length,
                prefill,
                decode,
            }))).await.unwrap_or(());
        }
        run_sender.send(Ok(Message::EndBatch)).await.unwrap_or(());
    }
    Ok(())
}

async fn prefill(
    sequence: String,
    batch_size: u32,
    decode_length: u32,
    client: &mut ShardedClient,
) -> Result<(Prefill, Batch), ClientError> {
    let requests = (0..batch_size)
        .map(|id| Request {
            id: id.into(),
            inputs: sequence.clone(),
            parameters: Some(NextTokenChooserParameters {
                temperature: 1.0,
                top_k: 0,
                top_p: 1.0,
                typical_p: 1.0,
                do_sample: false,
                seed: 0,
                repetition_penalty: 1.0,
                watermark: false,
            }),
            stopping_parameters: Some(StoppingCriteriaParameters {
                max_new_tokens: decode_length,
                stop_sequences: vec![],
                ignore_eos_token: true,
            }),
        })
        .collect();

    let batch = Batch {
        id: 0,
        requests,
        size: batch_size,
    };

    let start_time = Instant::now();
    let (_, decode_batch) = client.prefill(batch.clone()).await?;
    let latency = start_time.elapsed();
    let throughput = batch_size as f64
        / latency.as_secs_f64();

    let decode_batch = decode_batch.expect("decode_batch is None. This is a bug.");

    let step = Prefill {
        latency,
        throughput,
    };

    Ok((step, decode_batch))
}

async fn decode(
    batch: Batch,
    client: &mut ShardedClient,
) -> Result<Decode, ClientError> {
    let mut decode_length = 0;
    let start_time = Instant::now();
    let batch_size = batch.size;

    let mut next_batch = Some(batch);
    while let Some(batch) = next_batch {
        let result = client.decode(vec![batch]).await?;
        next_batch = result.1;
        decode_length += 1;
    }
    let latency = start_time.elapsed();
    let throughput = (batch_size * decode_length) as f64
        / latency.as_secs_f64();

    let step = Decode {
        decode_length,
        latency,
        throughput,
    };
    Ok(step)
}

fn create_sequence(sequence_length: u32, tokenizer: Tokenizer) -> String {
    let lorem_ipsum_length = tokenizer.encode(LOREM_IPSUM, true).unwrap().len();
    // Repeat lorem ipsum to cover sequence length
    let string_sequence =
        LOREM_IPSUM.repeat((0..sequence_length).step_by(lorem_ipsum_length).len());
    // Encode sequence
    let mut encoding = tokenizer.encode(string_sequence, true).unwrap();
    // Truncate to sequence_length
    encoding.truncate(sequence_length as usize, 0, TruncationDirection::Left);
    // Decode
    tokenizer
        .decode(Vec::from(encoding.get_ids()), false)
        .unwrap()
}
