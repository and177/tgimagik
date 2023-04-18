import torch

from dataclasses import dataclass
from opentelemetry import trace
from transformers import AutoTokenizer, AutoModelForCausalLM, PreTrainedTokenizerBase
from typing import Optional, Tuple, List, Type, Dict

from text_generation_server.models import Model
from text_generation_server.models.types import (
    Batch,
    PrefillTokens,
    Generation,
    GeneratedText,
)
from text_generation_server.pb import generate_pb2
from text_generation_server.utils import NextTokenChooser, StoppingCriteria, Sampling

tracer = trace.get_tracer(__name__)


@dataclass
class CausalLMBatch(Batch):
    batch_id: int
    requests: List[generate_pb2.Request]
    requests_idx_mapping: Dict[int, int]

    # Decoder values
    input_ids: torch.Tensor
    attention_mask: torch.Tensor
    position_ids: torch.Tensor
    past_key_values: Optional[List[Tuple]]

    # All tokens
    all_input_ids: List[torch.Tensor]

    # Lengths of all generations present in the batch
    input_lengths: List[int]
    offsets: List[Optional[int]]
    token_offsets: List[Optional[int]]

    # Generation helpers
    next_token_choosers: List[NextTokenChooser]
    stopping_criterias: List[StoppingCriteria]

    # Metadata used for padding
    max_input_length: int
    padding_right_offset: int

    # Past metadata
    keys_head_dim_last: bool = True

    def to_pb(self) -> generate_pb2.Batch:
        return generate_pb2.Batch(
            id=self.batch_id,
            requests=self.requests,
            size=len(self),
        )

    @classmethod
    def from_pb(
        cls,
        pb: generate_pb2.Batch,
        tokenizer: PreTrainedTokenizerBase,
        device: torch.device,
    ) -> "CausalLMBatch":
        inputs = []
        next_token_choosers = []
        stopping_criterias = []
        offsets = []
        token_offsets = []
        requests_idx_mapping = {}

        # Parse batch
        max_truncation = 0
        padding_right_offset = 0
        for i, r in enumerate(pb.requests):
            requests_idx_mapping[r.id] = i
            inputs.append(r.inputs)
            offsets.append(None)
            token_offsets.append(None)
            next_token_choosers.append(NextTokenChooser.from_pb(r.parameters, device))
            stopping_criteria = StoppingCriteria.from_pb(
                r.stopping_parameters, tokenizer
            )
            stopping_criterias.append(stopping_criteria)
            max_truncation = max(max_truncation, r.truncate)
            padding_right_offset = max(
                padding_right_offset, stopping_criteria.max_new_tokens
            )

        tokenized_inputs = tokenizer(
            inputs,
            return_tensors="pt",
            padding=True,
            return_token_type_ids=False,
            truncation=True,
            max_length=max_truncation,
        ).to(device)

        input_lengths = tokenized_inputs["attention_mask"].sum(1)
        max_input_length = input_lengths.max()

        input_ids = tokenized_inputs["input_ids"]
        # Allocate maximum attention_mask
        attention_mask = input_ids.new_zeros(
            (pb.size, max_input_length + padding_right_offset)
        )
        # Copy tokenizer attention_mask into fully allocated attention_mask
        attention_mask[:, :max_input_length] = tokenized_inputs["attention_mask"]

        position_ids = tokenized_inputs["attention_mask"].long().cumsum(-1) - 1
        position_ids.masked_fill_(tokenized_inputs["attention_mask"] == 0, 1)
        all_input_ids = tokenized_inputs["input_ids"].T.split(1, dim=1)

        return cls(
            batch_id=pb.id,
            requests=pb.requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=None,
            all_input_ids=list(all_input_ids),
            input_lengths=input_lengths.tolist(),
            offsets=offsets,
            token_offsets=token_offsets,
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_input_length=max_input_length.item(),
            padding_right_offset=padding_right_offset,
        )

    @tracer.start_as_current_span("filter")
    def filter(self, requests: List[generate_pb2.Request]) -> Optional["CausalLMBatch"]:
        if len(requests) == 0:
            raise ValueError("Batch must have at least one request")
        if len(requests) == len(self):
            return self

        keep_indices = []

        # New values after filtering
        requests_idx_mapping = {}
        input_lengths = []
        offsets = []
        token_offsets = []
        all_input_ids = []
        max_input_length = 0

        for i, r in enumerate(requests):
            idx = self.requests_idx_mapping[r.id]
            keep_indices.append(idx)
            requests_idx_mapping[r.id] = i

            offsets.append(self.offsets[idx])
            token_offsets.append(self.token_offsets[idx])
            all_input_ids.append(self.all_input_ids[idx])

            request_input_length = self.input_lengths[idx]
            input_lengths.append(request_input_length)
            max_input_length = max(max_input_length, request_input_length)

        # Replace metadata
        self.requests_idx_mapping = requests_idx_mapping
        self.input_lengths = input_lengths
        self.offsets = offsets
        self.token_offsets = token_offsets
        self.all_input_ids = all_input_ids
        self.max_input_length = max_input_length

        # Apply indices to input_ids, attention mask, past key values and other items that need to be cached
        self.input_ids = self.input_ids[keep_indices]
        self.attention_mask = self.attention_mask[keep_indices]
        self.position_ids = self.position_ids[keep_indices]
        # Force past to be of dim [self_size, num_heads, ...] for easy indexing
        self.past_key_values = [
            [t.view(len(self), -1, *t.shape[-2:])[keep_indices] for t in layer]
            for layer in self.past_key_values
        ]
        self.requests = requests
        self.next_token_choosers = [self.next_token_choosers[i] for i in keep_indices]
        self.stopping_criterias = [self.stopping_criterias[i] for i in keep_indices]

        return self

    @classmethod
    @tracer.start_as_current_span("concatenate")
    def concatenate(cls, batches: List["CausalLMBatch"]) -> "CausalLMBatch":
        # Used for padding
        total_batch_size = 0
        max_input_length = 0
        padding_right_offset = 0
        for batch in batches:
            total_batch_size += len(batch)
            max_input_length = max(max_input_length, batch.max_input_length)
            padding_right_offset = max(padding_right_offset, batch.padding_right_offset)

        # Batch attributes
        requests = []
        requests_idx_mapping = {}
        input_lengths = []
        offsets = []
        token_offsets = []
        all_input_ids = []
        next_token_choosers = []
        stopping_criterias = []

        # Batch tensors
        input_ids = None
        attention_mask = None
        position_ids = None
        past_key_values = []

        # Used for slicing correctly inside the tensors
        # Equivalent to a cumsum on batch sizes
        start_index = 0
        for i, batch in enumerate(batches):
            requests.extend(batch.requests)
            input_lengths.extend(batch.input_lengths)
            offsets.extend(batch.offsets)
            token_offsets.extend(batch.token_offsets)
            all_input_ids.extend(batch.all_input_ids)
            next_token_choosers.extend(batch.next_token_choosers)
            stopping_criterias.extend(batch.stopping_criterias)

            if i == 0:
                requests_idx_mapping = requests_idx_mapping
            else:
                for k, v in batch.requests_idx_mapping.items():
                    requests_idx_mapping[k] = v + start_index

            # Slicing end index for this batch
            end_index = start_index + len(batch)

            # We only concatenate batches that did at least one step
            if batch.past_key_values is None:
                raise ValueError("only concatenate prefilled batches")

            # Create empty tensor
            # input_ids is always of shape [batch_size, 1]
            # We do not need to pad it
            if input_ids is None:
                input_ids = batch.input_ids.new_empty((total_batch_size, 1))
            # Copy to correct indices
            input_ids[start_index:end_index] = batch.input_ids

            # Create padded tensor
            if attention_mask is None:
                attention_mask = batch.attention_mask.new_zeros(
                    (total_batch_size, max_input_length + padding_right_offset),
                )

            # We need to slice the attention mask to remove padding from previous steps
            # and to remove unused allocated space
            left_offset = max_input_length - batch.max_input_length
            batch_left_offset = (
                batch.attention_mask.shape[1]
                - batch.max_input_length
                - batch.padding_right_offset
            )
            attention_mask[
                start_index:end_index,
                left_offset:-padding_right_offset,
            ] = batch.attention_mask[
                :,
                batch_left_offset : -batch.padding_right_offset,
            ]

            # Create empty tensor
            # position_ids is always of shape [batch_size, 1]
            if position_ids is None:
                position_ids = batch.position_ids.new_empty((total_batch_size, 1))
            position_ids[start_index:end_index] = batch.position_ids

            for j, past in enumerate(batch.past_key_values):
                past_keys, past_values = past

                # Shenanigans to get dimensions because BLOOM outputs a past with a different shape
                # BLOOM Keys:   [batch_size * num_heads, head_dim, seq_length]
                # BLOOM Values: [batch_size * num_heads, seq_length, head_dim]
                past_keys = past_keys.view(len(batch), -1, *past_keys.shape[-2:])
                past_values = past_values.view(len(batch), -1, *past_values.shape[-2:])

                _, num_heads, padded_sequence_length, head_dim = past_values.shape

                padded_past_values_shape = (
                    total_batch_size,
                    num_heads,
                    max_input_length - 1,
                    head_dim,
                )

                if batch.keys_head_dim_last:
                    padded_past_keys_shape = padded_past_values_shape
                else:
                    # seq_length is last for BLOOM
                    padded_past_keys_shape = (
                        total_batch_size,
                        num_heads,
                        head_dim,
                        max_input_length - 1,
                    )

                # This will run only once per layer
                if j == len(past_key_values):
                    padded_past_keys = past_keys.new_zeros(padded_past_keys_shape)
                    padded_past_values = past_values.new_zeros(padded_past_values_shape)
                    past_key_values.append((padded_past_keys, padded_past_values))

                # We slice the past keys and values to remove the padding from previous batches
                if batch.keys_head_dim_last:
                    past_key_values[j][0][
                        start_index:end_index,
                        :,
                        -(batch.max_input_length - 1) :,
                        :,
                    ] = past_keys[:, :, -(batch.max_input_length - 1) :, :]
                else:
                    past_key_values[j][0][
                        start_index:end_index,
                        :,
                        :,
                        -(batch.max_input_length - 1) :,
                    ] = past_keys[:, :, :, -(batch.max_input_length - 1) :]

                past_key_values[j][1][
                    start_index:end_index, :, -(batch.max_input_length - 1) :, :
                ] = past_values[:, :, -(batch.max_input_length - 1) :, :]

            start_index += len(batch)

        return cls(
            batch_id=batches[0].batch_id,
            requests=requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            all_input_ids=all_input_ids,
            input_lengths=input_lengths,
            offsets=offsets,
            token_offsets=token_offsets,
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_input_length=max_input_length,
            padding_right_offset=padding_right_offset,
            keys_head_dim_last=batches[0].keys_head_dim_last,
        )

    def __len__(self):
        return len(self.requests)


class CausalLM(Model):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        quantize: bool = False,
        decode_buffer: int = 3,
    ):
        if torch.cuda.is_available():
            device = torch.device("cuda")
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32
        else:
            if quantize:
                raise ValueError("quantization is not available on CPU")

            device = torch.device("cpu")
            dtype = torch.float32

        tokenizer = AutoTokenizer.from_pretrained(
            model_id, revision=revision, padding_side="left", truncation_side="left"
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id,
            revision=revision,
            torch_dtype=dtype,
            device_map="auto" if torch.cuda.is_available() else None,
            load_in_8bit=quantize,
        ).eval()
        tokenizer.pad_token_id = (
            self.model.config.pad_token_id
            if self.model.config.pad_token_id is not None
            else self.model.config.eos_token_id
        )

        super(CausalLM, self).__init__(
            tokenizer=tokenizer, device=device, decode_buffer=decode_buffer
        )

    @property
    def batch_type(self) -> Type[CausalLMBatch]:
        return CausalLMBatch

    def decode(self, generated_ids: List[int]) -> str:
        return self.tokenizer.decode(
            generated_ids, skip_special_tokens=True, cleanup_tokenization_spaces=False
        )

    def forward(
        self, input_ids, attention_mask, position_ids, past_key_values: Optional = None
    ) -> Tuple[torch.Tensor, List[Tuple[torch.Tensor, torch.Tensor]]]:
        # Model Forward
        outputs = self.model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            use_cache=True,
        )
        return outputs.logits, outputs.past_key_values

    @tracer.start_as_current_span("generate_token")
    def generate_token(
        self, batch: CausalLMBatch
    ) -> Tuple[List[Generation], CausalLMBatch]:
        # slice the attention mask to the correct shape
        attention_mask = batch.attention_mask[:, : -batch.padding_right_offset]

        logits, past = self.forward(
            batch.input_ids,
            attention_mask,
            batch.position_ids,
            batch.past_key_values,
        )

        # Results
        generations: List[Generation] = []
        stopped = True

        # Zipped iterator
        iterator = zip(
            batch.requests,
            batch.input_lengths,
            batch.offsets,
            batch.token_offsets,
            logits,
            batch.next_token_choosers,
            batch.stopping_criterias,
            batch.all_input_ids,
        )

        # For each member of the batch
        for i, (
            request,
            input_length,
            offset,
            token_offset,
            logits,
            next_token_chooser,
            stopping_criteria,
            all_input_ids,
        ) in enumerate(iterator):
            # Select next token
            next_token_id, logprobs = next_token_chooser(
                all_input_ids.view(1, -1), logits
            )

            # Append next token to all tokens
            all_input_ids = torch.cat([all_input_ids, next_token_id])
            new_input_length = input_length + 1

            # Generated token
            next_token_logprob = logprobs[-1, next_token_id]
            next_token_id_squeezed = next_token_id.squeeze()
            next_token_text, offset, token_offset = self.decode_token(
                all_input_ids[:, 0], offset, token_offset
            )

            # Evaluate stopping criteria
            stop, reason = stopping_criteria(
                next_token_id_squeezed,
                next_token_text,
            )

            if stop:
                # Decode generated tokens
                output_text = self.decode(
                    all_input_ids[-stopping_criteria.current_tokens :, 0]
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
                # Keep request in the batch
                generated_text = None
                stopped = False

            # Prefill
            if stopping_criteria.current_tokens == 1:
                # Remove generated token to only have prefill and add nan for first prompt token
                prefill_logprobs = [float("nan")] + logprobs.gather(
                    1, all_input_ids[1:]
                ).squeeze(1)[-new_input_length:-1].tolist()
                prefill_token_ids = all_input_ids[-new_input_length:-1]
                prefill_texts = self.tokenizer.batch_decode(
                    prefill_token_ids,
                    clean_up_tokenization_spaces=False,
                    skip_special_tokens=False,
                )
                prefill_tokens = PrefillTokens(
                    prefill_token_ids, prefill_logprobs, prefill_texts
                )
            else:
                prefill_tokens = None

            generation = Generation(
                request.id,
                prefill_tokens,
                next_token_id_squeezed,
                next_token_logprob,
                next_token_text,
                next_token_id_squeezed.item() in self.all_special_ids,
                generated_text,
            )

            generations.append(generation)

            # Update values
            batch.input_ids[i] = next_token_id
            batch.all_input_ids[i] = all_input_ids
            batch.input_lengths[i] = new_input_length
            batch.offsets[i] = offset
            batch.token_offsets[i] = token_offset
            batch.max_input_length = max(batch.max_input_length, new_input_length)

        # Decrease right offset
        batch.padding_right_offset -= 1
        # Update attention_mask as we added a new token to input_ids
        batch.attention_mask[:, -batch.padding_right_offset] = 1

        # Update position_ids
        batch.position_ids = batch.position_ids[:, -1:] + 1

        # Update past key values
        batch.past_key_values = past

        return generations, batch if not stopped else None
