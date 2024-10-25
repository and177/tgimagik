#include <cstdlib>
#include <fstream>

#include <fmt/ranges.h>
#include <spdlog/spdlog.h>
#include <nvml.h>

#include "backend.h"
#include "hardware.h"

void huggingface::tgi::backends::InitializeBackend() {
    SPDLOG_INFO("Initializing Backend...");
    nvmlInit_v2();
    initTrtLlmPlugins();

    InitializeLogging();

    SPDLOG_INFO("Backend Executor Version: {}", tle::version());
    const auto numGpus = huggingface::hardware::cuda::GetNumDevices();
    if (numGpus.has_value()) {
        SPDLOG_INFO("Detected {:d} Nvidia GPU(s)", numGpus.value());
    } else {
        SPDLOG_WARN("Failed to detected Nvidia GPU(s) on the system");
    }
}

[[nodiscard]] tle::ParallelConfig GetParallelConfig(const size_t worldSize, std::string workerPath) {
    auto mode = tle::CommunicationMode::kLEADER;
    std::optional<tle::OrchestratorConfig> orchestratorConfig = std::nullopt;

    if (worldSize > 1) {
        SPDLOG_INFO("Detected sharded engine deployment, using orchestrator mode");
        mode = tle::CommunicationMode::kORCHESTRATOR;
        orchestratorConfig = std::make_optional<tle::OrchestratorConfig>(true, workerPath, nullptr, true);
    } else {
        SPDLOG_INFO("Detected single engine deployment, using leader mode");
    }

    return tle::ParallelConfig(tle::CommunicationType::kMPI, mode, std::nullopt, std::nullopt, orchestratorConfig);
}

[[nodiscard]]
tle::ExecutorConfig huggingface::tgi::backends::GetExecutorConfig(const json &config, const std::string &workerPath) {
    tle::ExecutorConfig execConfig(/* maxBeamWidth = */ 1);

    // Retrieve the compute capabilities to enable some options at runtime
    const auto computeCapabilities = huggingface::hardware::cuda::GetCudaComputeCapabilities();

    // Single engine (TP = PP = 1) -> using leader mode (no MPI involved)
    const auto worldSize = config["/pretrained_config/mapping/world_size"_json_pointer].get<size_t>();
    execConfig.setParallelConfig(GetParallelConfig(worldSize, workerPath));

    // Define some configuration variables
    execConfig.setKvCacheConfig(tle::KvCacheConfig(true));
    execConfig.setEnableChunkedContext(computeCapabilities.isPostAmpere());
    return execConfig;
}

tle::SamplingConfig huggingface::tgi::backends::GetSamplingConfig(
        const uint32_t topK,
        const float_t topP,
        const float_t temperature,
        const float_t repetition_penalty,
        const float_t frequency_penalty,
        const uint64_t seed) noexcept {

    return tle::SamplingConfig(
            1,  // TGI only use a single beam
            topK,
            topP,
            std::nullopt,
            std::nullopt,
            std::nullopt,
            seed,
            temperature,
            temperature,
            std::nullopt,
            repetition_penalty,
            std::nullopt,
            frequency_penalty
    );
}

huggingface::tgi::backends::TensorRtLlmBackend::TensorRtLlmBackend(
        const std::filesystem::path &enginesFolder,
        const std::filesystem::path &executorWorker
) :
        config(json::parse(std::ifstream(enginesFolder / "config.json"))),
        executor(enginesFolder, tensorrt_llm::executor::ModelType::kDECODER_ONLY,
                 GetExecutorConfig(config, executorWorker.string())) {
    SPDLOG_INFO(FMT_STRING("Engine (version={})"), config["/version"_json_pointer].get_ref<const std::string &>());

    // Cache variables
    maxNumTokens = config["/build_config/max_num_tokens"_json_pointer].get<uint32_t>();

    // Attempt to discover stopWords from the generation_config.json
    if (auto generationConfigPath = enginesFolder / "generation_config.json"; exists(generationConfigPath)) {
        const auto generationConfig = json::parse(std::ifstream(generationConfigPath));
        if (const auto eosTokenIds = generationConfig["/eos_token_ids"_json_pointer]; eosTokenIds.is_array()) {
            SPDLOG_INFO(FMT_STRING("Found {:d} EOS tokens"), eosTokenIds.size());
            stopWords = std::list<decltype(stopWords)::value_type>(eosTokenIds.size());

            std::transform(eosTokenIds.cbegin(), eosTokenIds.cend(), stopWords.begin(),
                           [](const auto tokenIdObj) -> decltype(stopWords)::value_type {
                               const auto tokenId = tokenIdObj.template get<tle::TokenIdType>();
                               return {tokenId};
                           });
        }
    } else {
        SPDLOG_INFO("No EOS tokens found, generation_config.json doesn't exist");
        stopWords = {};
    }
}

[[nodiscard("Returned number of requests needs to be consumed")]]
size_t huggingface::tgi::backends::TensorRtLlmBackend::NumResponsesReady() const {
    const auto numResponses = executor.getNumResponsesReady();

#ifndef NDEBUG
    if(numResponses > 0) SPDLOG_INFO(FMT_STRING("Num responses ready: {:d}"), numResponses);
#endif

    return numResponses;
}

[[nodiscard("Returned request id needs to be provided back to gather generated tokens")]]
tle::IdType huggingface::tgi::backends::TensorRtLlmBackend::Submit(
        const std::vector<tle::TokenIdType> &tokens,
        const uint32_t maxNewTokens,
        const int32_t topK,
        const float_t topP,
        const float_t temperature,
        const float_t repetitionPenalty,
        const float_t frequencyPenalty,
        const uint64_t seed
) {
    const auto maxNewTokensChecked = std::min(maxNewTokens, static_cast<uint32_t>(maxNumTokens - tokens.size()));
#ifndef NDEBUG
    {
        const auto &iterations = executor.getLatestIterationStats();
        const auto &lastIteration = iterations.front();

        SPDLOG_DEBUG(FMT_EXECUTOR_STATS, fmt::join(tokens, ", "), lastIteration.numActiveRequests);
        SPDLOG_DEBUG(FMT_SAMPLING_CONFIG, topK, topP, temperature, repetitionPenalty, frequencyPenalty, seed);
        SPDLOG_DEBUG(FMT_STRING("Asking for max_new_tokens={:d}"), maxNewTokensChecked);
    }
#endif

    const auto sampling = GetSamplingConfig(topK, topP, temperature, repetitionPenalty, frequencyPenalty, seed);

    // Build the request
    auto request = tle::Request{tokens, CAST_SIZETYPE(maxNewTokensChecked), true, sampling, OUTPUT_CONFIG};
    request.setStopWords(stopWords);

    // Submit to the executor for batching
    return executor.enqueueRequest(request);
}

std::vector<tle::Response> huggingface::tgi::backends::TensorRtLlmBackend::PullNewTokens() {
    return executor.awaitResponses();
}
