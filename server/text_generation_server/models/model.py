import torch

from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, TypeVar, Type
from transformers import PreTrainedTokenizerBase

from text_generation_server.models.types import Batch, GeneratedText
from text_generation_server.pb.generate_pb2 import InfoResponse

B = TypeVar("B", bound=Batch)


class Model(ABC):
    def __init__(
        self,
        model: torch.nn.Module,
        tokenizer: PreTrainedTokenizerBase,
        requires_padding: bool,
        dtype: torch.dtype,
        device: torch.device,
        decode_buffer: int = 4,
        rank: int = 0,
        world_size: int = 1,
    ):
        if decode_buffer < 1:
            raise ValueError("decode_buffer must be >= 1")

        self.model = model.eval().to(device)
        self.tokenizer = tokenizer
        self.all_special_ids = set(tokenizer.all_special_ids)
        self.requires_padding = requires_padding
        self.dtype = dtype
        self.device = device
        self.decode_buffer = decode_buffer
        self.rank = rank
        self.world_size = world_size
        self.check_initialized()

    @property
    def info(self) -> InfoResponse:
        return InfoResponse(
            requires_padding=self.requires_padding,
            dtype=str(self.dtype),
            device_type=self.device.type,
        )

    @property
    @abstractmethod
    def batch_type(self) -> Type[B]:
        raise NotImplementedError

    @abstractmethod
    def generate_token(self, batch: B) -> Tuple[List[GeneratedText], Optional[B]]:
        raise NotImplementedError

    def decode_token(
        self,
        all_input_ids: List[int],
        prefix_offset: int = 0,
        read_offset: int = 0,
    ) -> Tuple[str, int, int]:
        """Hack to hopefully support generate_stream for the maximum number of tokenizers"""

        # Compatibility layer for old None values.
        if prefix_offset is None:
            prefix_offset = 0
        if read_offset is None:
            read_offset = 0

        prefix_text = self.tokenizer.decode(all_input_ids[prefix_offset:read_offset])
        new_text = self.tokenizer.decode(all_input_ids[prefix_offset:])

        if len(new_text) > len(prefix_text) and "�" not in new_text:
            new_text = new_text[len(prefix_text) :]
            return new_text, read_offset, len(all_input_ids)
        else:
            return "", prefix_offset, read_offset

    def check_initialized(self):
        uninitialized_parameters = []
        for n, p in self.model.named_parameters():
            if p.data.device == torch.device("meta"):
                uninitialized_parameters.append(n)
        if uninitialized_parameters:
            raise RuntimeError(
                f"found uninitialized parameters in model {self.__class__.__name__}: {uninitialized_parameters}"
            )
