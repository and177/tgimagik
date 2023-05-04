import torch
import torch.distributed

import numpy as np

from torch.nn import functional as F

from dataclasses import dataclass
from opentelemetry import trace
from transformers import AutoTokenizer, PreTrainedTokenizerBase, PreTrainedModel
from typing import Optional, Tuple, List, Type, Union, Dict

from text_generation_server.models import Model
from text_generation_server.models.types import (
    Batch,
    PrefillTokens,
    Generation,
    GeneratedText,
)
from text_generation_server.pb import generate_pb2
from text_generation_server.utils import (
    NextTokenChooser,
    StoppingCriteria,
    Sampling,
)

tracer = trace.get_tracer(__name__)


@dataclass
class FlashCausalLMBatch(Batch):
    batch_id: int
    requests: List[generate_pb2.Request]
    # request id -> idx in list mapping
    requests_idx_mapping: Dict[int, int]

    # Decoder values
    input_ids: torch.Tensor
    position_ids: torch.Tensor
    # cumulative sequence lengths
    cu_seqlens: torch.Tensor
    cu_seqlens_q: Optional[torch.Tensor]
    max_seqlen: int
    past_key_values: Optional[torch.Tensor]

    # All tokens
    all_input_ids: List[List[int]]
    all_input_ids_tensor: List[torch.Tensor]

    # Lengths of all generations present in the batch
    input_lengths: List[int]
    offsets: List[Optional[int]]
    token_offsets: List[Optional[int]]

    # Generation helpers
    next_token_choosers: List[NextTokenChooser]
    stopping_criterias: List[StoppingCriteria]

    # Maximum number of tokens this batch will grow to
    max_tokens: int

    def to_pb(self) -> generate_pb2.Batch:
        return generate_pb2.Batch(
            id=self.batch_id,
            requests=self.requests,
            size=len(self),
            max_tokens=self.max_tokens,
        )

    @classmethod
    def from_pb(
        cls,
        pb: generate_pb2.Batch,
        tokenizer: PreTrainedTokenizerBase,
        device: torch.device,
    ) -> "FlashCausalLMBatch":
        position_ids = []
        cu_seqlens = [0]
        max_seqlen = 0

        input_lengths = []
        offsets = []
        token_offsets = []
        all_input_ids = []
        requests_idx_mapping = {}

        next_token_choosers = []
        stopping_criterias = []

        # Cumulative length
        cumulative_length = 0

        max_tokens = 0

        # Parse batch
        for i, r in enumerate(pb.requests):
            # request id -> idx in list mapping
            requests_idx_mapping[r.id] = i

            tokenized_input = tokenizer(
                r.inputs, truncation=True, max_length=r.truncate
            )["input_ids"]

            input_length = len(tokenized_input)
            max_seqlen = max(max_seqlen, input_length)
            input_lengths.append(input_length)

            offsets.append(None)
            token_offsets.append(None)

            all_input_ids.append(tokenized_input)

            # Position ids
            position_ids.append(np.arange(0, input_length))

            # Add cumulative lengths of all previous inputs
            cu_seqlens.append(cumulative_length + input_length)

            next_token_choosers.append(NextTokenChooser.from_pb(r.parameters, device))

            stopping_criteria = StoppingCriteria.from_pb(
                r.stopping_parameters, tokenizer
            )
            max_new_tokens = stopping_criteria.max_new_tokens
            stopping_criterias.append(stopping_criteria)

            # Update
            cumulative_length += input_length
            max_tokens += input_length + max_new_tokens

        input_ids = torch.tensor(
            np.concatenate(all_input_ids), dtype=torch.int32, device=device
        )
        position_ids = torch.tensor(
            np.concatenate(position_ids), dtype=torch.int32, device=device
        )
        cu_seqlens = torch.tensor(cu_seqlens, device=device, dtype=torch.int32)

        return cls(
            batch_id=pb.id,
            requests=pb.requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            position_ids=position_ids,
            cu_seqlens=cu_seqlens,
            cu_seqlens_q=None,
            max_seqlen=max_seqlen,
            past_key_values=None,
            input_lengths=input_lengths,
            offsets=offsets,
            token_offsets=token_offsets,
            all_input_ids=all_input_ids,
            all_input_ids_tensor=[],
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_tokens=max_tokens,
        )

    @tracer.start_as_current_span("filter")
    def filter(self, requests: List[generate_pb2.Request]) -> "FlashCausalLMBatch":
        if len(requests) == 0:
            raise ValueError("Batch must have at least one request")
        # We assume that if len(requests) == len(self) then the requests are the same
        if len(requests) == len(self):
            return self

        single_request = len(requests) == 1

        # Cumulative length
        cumulative_length = 0

        # New values after filtering
        requests_idx_mapping = {}

        input_ids = []
        position_ids = []
        cu_seqlens = [0]
        max_seqlen = 0
        past_key_values = []

        all_input_ids = []
        all_input_ids_tensor = []

        input_lengths = []
        offsets = []
        token_offsets = []

        next_token_choosers = []
        stopping_criterias = []

        max_tokens = 0

        for i, r in enumerate(requests):
            idx = self.requests_idx_mapping[r.id]
            requests_idx_mapping[r.id] = i

            # Get length
            request_input_length = self.input_lengths[idx]

            input_ids.append(self.input_ids[idx])
            position_ids.append(self.position_ids[idx])
            cu_seqlens.append(cumulative_length + request_input_length)
            max_seqlen = max(max_seqlen, request_input_length)
            # True index for past
            past_key_values.append(self.past_key_values[2 * idx])

            if not single_request:
                # Add one padding
                past_key_values.append(self.past_pad)

            all_input_ids.append(self.all_input_ids[idx])
            all_input_ids_tensor.append(self.all_input_ids_tensor[idx])

            input_lengths.append(request_input_length)
            offsets.append(self.offsets[idx])
            token_offsets.append(self.token_offsets[idx])

            next_token_choosers.append(self.next_token_choosers[idx])

            stopping_criteria = self.stopping_criterias[idx]
            stopping_criterias.append(stopping_criteria)

            cumulative_length += request_input_length
            max_tokens += request_input_length + (
                stopping_criteria.max_new_tokens - stopping_criteria.current_tokens
            )

        if single_request:
            # Preallocate tensor for bs = 1 case
            past_key_values = torch.nn.functional.pad(
                past_key_values[0],
                (
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    stopping_criterias[0].max_new_tokens
                    - stopping_criterias[0].current_tokens,
                ),
            )

        return FlashCausalLMBatch(
            batch_id=self.batch_id,
            past_pad=self.past_pad,
            requests=requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            position_ids=position_ids,
            cu_seqlens=cu_seqlens,
            max_seqlen=max_seqlen,
            past_key_values=past_key_values,
            input_lengths=input_lengths,
            offsets=offsets,
            token_offsets=token_offsets,
            all_input_ids=all_input_ids,
            all_input_ids_tensor=all_input_ids_tensor,
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_tokens=max_tokens,
        )

    @classmethod
    @tracer.start_as_current_span("concatenate")
    def concatenate(cls, batches: List["FlashCausalLMBatch"]) -> "FlashCausalLMBatch":
        # Batch attributes
        requests = []
        requests_idx_mapping = {}

        input_ids = []
        position_ids = []
        cu_seqlens = [0]
        max_seqlen = 0
        past_key_values = []

        all_input_ids = []
        all_input_ids_tensor = []

        input_lengths = []
        offsets = []
        token_offsets = []

        next_token_choosers = []
        stopping_criterias = []

        # Cumulative length
        cumulative_batch_size = 0
        cumulative_length = 0
        max_tokens = 0

        for i, batch in enumerate(batches):
            requests.extend(batch.requests)

            if i == 0:
                requests_idx_mapping = batch.requests_idx_mapping
            else:
                # We need to offset the mapping for each batch by the cumulative batch size
                for k, v in batch.requests_idx_mapping.items():
                    requests_idx_mapping[k] = v + cumulative_batch_size

            input_ids.extend(batch.input_ids)
            position_ids.extend(batch.position_ids)
            # Add cumulative lengths of all previous inputs
            cu_seqlens.extend([l + cumulative_length for l in batch.cu_seqlens[1:]])
            max_seqlen = max(max_seqlen, batch.max_seqlen)

            if len(batch) != 1:
                past_key_values.extend(batch.past_key_values)
            else:
                # past was pre-allocated for this batch
                # We need to slice to remove the padding
                past_key_values.append(
                    batch.past_key_values[:, : batch.input_lengths[0]]
                )
                # Add one padding
                past_key_values.append(batch.past_pad)

            all_input_ids.extend(batch.all_input_ids)
            all_input_ids_tensor.extend(batch.all_input_ids_tensor)

            input_lengths.extend(batch.input_lengths)
            offsets.extend(batch.offsets)
            token_offsets.extend(batch.token_offsets)

            next_token_choosers.extend(batch.next_token_choosers)
            stopping_criterias.extend(batch.stopping_criterias)

            # Update
            cumulative_length += batch.cu_seqlens[-1]
            cumulative_batch_size += len(batch)
            max_tokens += batch.max_tokens

        return FlashCausalLMBatch(
            batch_id=batches[0].batch_id,
            past_pad=batches[0].past_pad,
            requests=requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            position_ids=position_ids,
            cu_seqlens=cu_seqlens,
            max_seqlen=max_seqlen,
            past_key_values=past_key_values,
            input_lengths=input_lengths,
            offsets=offsets,
            token_offsets=token_offsets,
            all_input_ids=all_input_ids,
            all_input_ids_tensor=all_input_ids_tensor,
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_tokens=max_tokens,
        )

    def __len__(self):
        return len(self.requests)


class FlashCausalLM(Model):
    def __init__(
        self,
        model_cls: Type[PreTrainedModel],
        model_id: str,
        revision: Optional[str] = None,
        quantize: bool = False,
        decode_buffer: int = 3,
    ):
        if torch.cuda.is_available():
            device = torch.device("cuda")
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        else:
            raise NotImplementedError("FlashCausalLM is only available on GPU")

        tokenizer = AutoTokenizer.from_pretrained(
            model_id, revision=revision, padding_side="left", truncation_side="left"
        )
        self.model = (
            model_cls.from_pretrained(
                model_id,
                revision=revision,
                torch_dtype=dtype,
                load_in_8bit=quantize,
            )
            .eval()
            .to(device)
        )

        super(FlashCausalLM, self).__init__(
            tokenizer=tokenizer,
            requires_padding=False,
            dtype=dtype,
            device=device,
            decode_buffer=decode_buffer,
        )

    @property
    def batch_type(self) -> Type[FlashCausalLMBatch]:
        return FlashCausalLMBatch

    def decode(self, generated_ids: Union[torch.Tensor, List[int]]) -> str:
        return self.tokenizer.decode(
            generated_ids, skip_special_tokens=True, cleanup_tokenization_spaces=False
        )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlens: torch.Tensor,
        cu_seqlens_q: Optional[torch.Tensor],
        max_s: int,
        past_key_values: Optional = None,
        pre_allocate_past_size: Optional[int] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Model Forward
        return self.model.forward(
            input_ids=input_ids,
            position_ids=position_ids,
            cu_seqlens=cu_seqlens,
            cu_seqlens_q=cu_seqlens_q,
            max_s=max_s,
            past_key_values=past_key_values,
            pre_allocate_past_size=pre_allocate_past_size,
        )

    @tracer.start_as_current_span("generate_token")
    def generate_token(
        self, batch: FlashCausalLMBatch
    ) -> Tuple[List[Generation], Optional[FlashCausalLMBatch]]:
        prefill = batch.past_key_values is None

        # Shortcut when batch_size == 1
        if prefill and len(batch) == 1:
            # Ask to pre-allocate kv to its max size
            # == number of tokens + max_new_tokens
            pre_allocate_past_size = (
                batch.input_lengths[0] + batch.stopping_criterias[0].max_new_tokens
            )
        else:
            pre_allocate_past_size = None

        out, present = self.forward(
            batch.input_ids,
            batch.position_ids,
            batch.cu_seqlens,
            batch.cu_seqlens_q,
            batch.max_seqlen,
            batch.past_key_values,
            pre_allocate_past_size,
        )

        if prefill:
            # Compute logprobs for the whole batch
            prefill_logprobs_tensor = torch.log_softmax(out, -1)
        else:
            prefill_logprobs_tensor = None

        # Used to slice next batch past
        past_indices = []
        prefill_logprobs = []
        next_token_logprobs = []

        # Cumulative length
        cumulative_length = 0

        # Results
        generations: List[Generation] = []
        stopped = True

        # Zipped iterator
        iterator = zip(
            batch.input_lengths,
            batch.next_token_choosers,
            batch.stopping_criterias,
            batch.all_input_ids,
        )

        # For each member of the batch
        for i, (
            input_length,
            next_token_chooser,
            stopping_criteria,
            all_input_ids,
        ) in enumerate(iterator):
            # Indexing metadata
            start_index = cumulative_length
            end_index = cumulative_length + input_length

            if prefill:
                # Prefill mode
                # out is of shape [cumulative_sequence_lengths, vocab_size]
                # only take last token logit
                logits = out[end_index - 1 : end_index]
                all_input_ids_tensor = F.pad(
                    batch.input_ids[start_index:end_index],
                    (0, stopping_criteria.max_new_tokens),
                )
                batch.all_input_ids_tensor.append(all_input_ids_tensor)

                batch.position_ids[i] = input_length
                prefill_logprobs.append(
                    prefill_logprobs_tensor[start_index:end_index]
                    .gather(
                        1,
                        all_input_ids_tensor[1:input_length]
                        .unsqueeze(1)
                        .to(torch.int64),
                    )
                    .squeeze(1)[:-1]
                )
            else:
                # Decode mode
                # out is of shape [batch_size, vocab_size]
                logits = out[i].unsqueeze(0)

            all_input_ids_tensor = batch.all_input_ids_tensor[i]

            # Select next token
            next_token_id, logprob = next_token_chooser(
                all_input_ids_tensor[None, :input_length], logits
            )
            next_token_id_squeezed = next_token_id.squeeze()
            all_input_ids_tensor[input_length] = next_token_id_squeezed
            past_indices.extend([j for j in range(start_index + i, end_index + i)])

            batch.input_ids[i] = next_token_id_squeezed
            next_token_logprobs.append(logprob[-1, next_token_id])

            cumulative_length += input_length

        if prefill:
            batch.input_ids = batch.input_ids[: len(batch)]
            batch.position_ids = batch.position_ids[: len(batch)]
            batch.cu_seqlens_q = torch.arange(
                0, len(batch) + 1, device=self.device, dtype=torch.int32
            )
        else:
            batch.position_ids += 1

        # Initialize past_key_values in prefill
        if prefill and len(batch) == 1:
            # present is already pre-padded
            batch.past_key_values = present


        batch.cu_seqlens = batch.cu_seqlens + batch.cu_seqlens_q

        if len(batch) > 1:
            prefill_logprobs = torch.cat(prefill_logprobs) if prefill else None
            next_token_logprobs = torch.cat(next_token_logprobs)

            batch.past_key_values = present.new_empty(
                (
                    present.shape[0],
                    present.shape[1] + len(batch.requests),
                    *present.shape[2:],
                )
            )
            batch.past_key_values[:, past_indices] = present

            prefill_logprobs = prefill_logprobs.to("cpu") if prefill else None
            next_token_logprobs = next_token_logprobs.to("cpu")
        else:
            prefill_logprobs = prefill_logprobs[0] if prefill else None
            next_token_logprobs = next_token_logprobs[0]

        next_token_ids = batch.input_ids.to("cpu")

        prefill_logprobs_cumulative_length = 0

        # Zipped iterator
        iterator = zip(
            batch.requests,
            batch.input_lengths,
            batch.offsets,
            batch.token_offsets,
            batch.next_token_choosers,
            batch.stopping_criterias,
            batch.all_input_ids,
            batch.all_input_ids_tensor,
            next_token_ids,
            next_token_logprobs,
        )

        # For each member of the batch
        for i, (
            request,
            input_length,
            offset,
            token_offset,
            next_token_chooser,
            stopping_criteria,
            all_input_ids,
            all_input_ids_tensor,
            next_token_id,
            next_token_logprob,
        ) in enumerate(iterator):
            next_token_id_item = next_token_id.item()

            # Append next token to all tokens
            all_input_ids.append(next_token_id_item)

            # Generated token
            next_token_text, offset, token_offset = self.decode_token(
                all_input_ids,
                offset,
                token_offset,
            )

            # Evaluate stopping criteria
            stop, reason = stopping_criteria(
                next_token_id_item,
                next_token_text,
            )

            if stop:
                # Decode generated tokens
                output_text = self.decode(
                    all_input_ids[-stopping_criteria.current_tokens :]
                )
                # Get seed
                if isinstance(next_token_chooser.choice, Sampling):
                    seed = next_token_chooser.choice.seed
                else:
                    seed = None

                generated_text = GeneratedText(
                    output_text, stopping_criteria.current_tokens, reason, seed
                )
            else:
                stopped = False
                generated_text = None

            # Prefill
            if prefill:
                start_index = prefill_logprobs_cumulative_length
                end_index = prefill_logprobs_cumulative_length + input_length - 1

                # Remove generated token to only have prefill and add nan for first prompt token
                request_prefill_logprobs = [float("nan")] + prefill_logprobs[start_index:end_index].tolist()
                prefill_token_ids = all_input_ids[:-1]
                prefill_texts = self.tokenizer.batch_decode(
                    prefill_token_ids,
                    clean_up_tokenization_spaces=False,
                    skip_special_tokens=False,
                )
                prefill_tokens = PrefillTokens(
                    prefill_token_ids, request_prefill_logprobs, prefill_texts
                )

                prefill_logprobs_cumulative_length += input_length - 1
            else:
                prefill_tokens = None

            generation = Generation(
                request.id,
                prefill_tokens,
                next_token_id_item,
                next_token_logprob.item(),
                next_token_text,
                next_token_id_item in self.all_special_ids,
                generated_text,
            )

            generations.append(generation)
            new_input_length = input_length + 1

            # Update values
            batch.input_lengths[i] = new_input_length
            batch.offsets[i] = offset
            batch.token_offsets[i] = token_offset
            batch.all_input_ids[i] = all_input_ids
            batch.max_seqlen = max(batch.max_seqlen, new_input_length)

        # No need to return a batch if we know that all requests stopped
        return generations, batch if not stopped else None
