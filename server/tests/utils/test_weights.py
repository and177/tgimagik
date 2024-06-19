import torch
from text_generation_server.utils.weights import Weights
from text_generation_server.layers.gptq import GPTQWeight
from text_generation_server.layers.exl2 import Exl2Weight
from text_generation_server.layers.marlin import MarlinWeight
from types import SimpleNamespace
from typing import List, Optional, Dict, Union
from pathlib import Path

dummy_file_system = {
    "test_weights": {
        "layer.0.weight": torch.tensor(
            [
                [1, 2],
                [3, 4],
            ],
            dtype=torch.float32,
        ),
    },
    "test_weights_2": {
        "layer.1337.weight": torch.tensor(
            [
                [1, 2, 3, 4],
                [5, 6, 7, 8],
            ],
            dtype=torch.float32,
        ),
    },
    "test_get_multi_weights_col_packed": {
        "col_packed.weight": torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
        "col_packed_2.weight": torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
    },
    "test_get_multi_weights_row": {
        "row_packed.weight": torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
    },
    "test_get_multi_weights_row_gptq": {
        "weight.qweight": torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
        "weight.g_idx": torch.tensor([1.0], dtype=torch.float32),
        "weight.qzeros": torch.tensor([[1.0], [2.0]], dtype=torch.float32),
        "weight.scales": torch.tensor([8], dtype=torch.int32),
        #
        "gptq_bits": torch.tensor([8], dtype=torch.float32),
        "gptq_groupsize": torch.tensor([4], dtype=torch.float32),
    },
    "test_get_multi_weights_row_exl2": {
        "weight.q_weight": torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
        "weight.q_scale": torch.tensor([8], dtype=torch.int32),
        "weight.q_invperm": torch.tensor([1.0], dtype=torch.float32),
        "weight.q_scale_max": 8,
        "weight.q_groups": torch.tensor([4], dtype=torch.int32),
    },
    "test_get_multi_weights_row_marlin": {
        "weight.scales": torch.tensor([8], dtype=torch.float16),
        "weight.B": torch.tensor([[1, 2], [3, 4]], dtype=torch.int32),
        "weight.s": torch.tensor([0.5], dtype=torch.float16),
    },
}


class MockSlice:
    def __init__(self, tensor):
        self.tensor = tensor

    def get_shape(self):
        return self.tensor.shape

    def __getitem__(self, idx):
        return self.tensor[idx]


def mock_get_slice(tensor_name, filename):
    tensor = dummy_file_system[filename][tensor_name]
    return MockSlice(tensor)


def mock_handle(filename, device, dtype):
    return SimpleNamespace(
        get_slice=lambda tensor_name: mock_get_slice(tensor_name, filename)
    )


class MockSafeOpen:
    def __init__(self, filename, framework, dummy_fs):
        self.filename = filename
        self.framework = framework
        self.dummy_fs = dummy_fs

    def keys(self):
        return list(self.dummy_fs[self.filename].keys())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockWeights(Weights):
    def __init__(
        self,
        filenames: List[Union[Path, str]],
        device,
        dtype,
        process_group,
        dummy_fs,
        aliases: Optional[Dict[str, List[str]]] = None,
        prefix: Optional[str] = None,
    ):
        routing = {}
        self.dummy_fs = dummy_fs
        for filename in filenames:
            with MockSafeOpen(filename, framework="pytorch", dummy_fs=dummy_fs) as f:
                for k in f.keys():
                    if k in routing:
                        raise RuntimeError(
                            f"Key {k} was found in multiple files: {filename} and {routing[k]}"
                        )
                    routing[k] = filename
        if aliases is None:
            aliases = {}
        self.aliases = aliases
        self.routing = routing
        self.device = device
        self.dtype = dtype
        self.process_group = process_group
        self.prefix = prefix
        self._handles = {}

    def _get_handle(self, filename: Union[Path, str]):
        if filename in self._handles:
            return self._handles[filename]
        else:
            handle = mock_handle(filename, self.device, self.dtype)
            self._handles[filename] = handle
            return handle

    def get_shape(self, tensor_name: str):
        filename, _ = self.get_filename(tensor_name)
        handle = self._get_handle(filename)
        return handle.get_slice(tensor_name).get_shape()

    def get_tensor(self, tensor_name: str):
        filename, _ = self.get_filename(tensor_name)
        handle = self._get_handle(filename)
        return handle.get_slice(tensor_name).tensor


dummy_process_group = SimpleNamespace(rank=lambda: 0, size=lambda: 1)


def test_weights():
    weights = MockWeights(
        [
            "test_weights",
            "test_weights_2",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )
    assert weights.get_shape("layer.0.weight") == (2, 2)
    assert weights.get_tensor("layer.1337.weight").shape == (2, 4)


def test_get_tensor():
    weights = MockWeights(
        [
            "test_weights",
            "test_weights_2",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )
    assert torch.allclose(
        weights.get_tensor("layer.0.weight"),
        torch.tensor(
            [
                [1, 2],
                [3, 4],
            ],
            dtype=torch.float32,
        ),
    )
    assert torch.allclose(
        weights.get_tensor("layer.1337.weight"),
        torch.tensor(
            [
                [1, 2, 3, 4],
                [5, 6, 7, 8],
            ],
            dtype=torch.float32,
        ),
    )


def test_get_weights_col_packed():

    weights = MockWeights(
        [
            "test_get_multi_weights_col_packed",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "col_packed"
    quantize = None
    block_sizes = 1

    w = weights.get_weights_col_packed(
        prefix=prefix,
        quantize=quantize,
        block_sizes=block_sizes,
    )

    prefix = "col_packed"
    quantize = None
    block_sizes = 1

    assert torch.allclose(
        w,
        torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
    )


def test_get_multi_weights_col_packed():
    weights = MockWeights(
        [
            "test_get_multi_weights_col_packed",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefixes = ["col_packed", "col_packed_2"]
    quantize = None

    w = weights.get_multi_weights_col(
        prefixes=prefixes,
        quantize=quantize,
        dim=0,
    )

    assert torch.allclose(
        w,
        torch.tensor(
            [
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
                [1, 2],
                [3, 4],
                [5, 6],
                [7, 8],
            ],
            dtype=torch.float32,
        ),
    )


def test_get_multi_weights_row():
    weights = MockWeights(
        [
            "test_get_multi_weights_row",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "row_packed"
    quantize = None

    w = weights.get_multi_weights_row(
        prefix=prefix,
        quantize=quantize,
    )

    assert torch.allclose(
        w,
        torch.tensor(
            [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]],
            dtype=torch.float32,
        ),
    )


def test_get_multi_weights_row_gptq():
    weights = MockWeights(
        [
            "test_get_multi_weights_row_gptq",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "weight"
    quantize = "gptq"

    w = weights.get_multi_weights_row(
        prefix=prefix,
        quantize=quantize,
    )

    expected_weight = GPTQWeight(
        qweight=torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]),
        qzeros=torch.tensor([[1.0], [2.0]], dtype=torch.float32),
        scales=torch.tensor([8], dtype=torch.int32),
        g_idx=torch.tensor([1.0], dtype=torch.float32),
        bits=torch.tensor([8], dtype=torch.float32),
        groupsize=torch.tensor([4], dtype=torch.float32),
        use_exllama=False,
    )

    assert torch.allclose(w.qweight, expected_weight.qweight), "qweight mismatch"
    assert torch.allclose(w.qzeros, expected_weight.qzeros), "qzeros mismatch"
    assert torch.allclose(w.scales, expected_weight.scales), "scales mismatch"
    assert torch.allclose(w.g_idx, expected_weight.g_idx), "g_idx mismatch"
    assert w.bits == expected_weight.bits, "bits mismatch"
    assert w.groupsize == expected_weight.groupsize, "groupsize mismatch"
    assert w.use_exllama == expected_weight.use_exllama, "use_exllama mismatch"


def test_get_multi_weights_row_exl2():
    weights = MockWeights(
        [
            "test_get_multi_weights_row_exl2",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "weight"
    quantize = "exl2"

    w = weights.get_multi_weights_row(
        prefix=prefix,
        quantize=quantize,
    )

    expected_weight = Exl2Weight(
        q_weight=torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]),
        q_scale=torch.tensor([8], dtype=torch.int32),
        q_invperm=torch.tensor([1.0], dtype=torch.float32),
        q_scale_max=8,
        q_groups=torch.tensor([4], dtype=torch.int32),
    )

    assert torch.allclose(w.q_weight, expected_weight.q_weight), "q_weight mismatch"
    assert torch.allclose(w.q_scale, expected_weight.q_scale), "q_scale mismatch"
    assert torch.allclose(w.q_invperm, expected_weight.q_invperm), "q_invperm mismatch"
    assert w.q_scale_max == expected_weight.q_scale_max
    assert torch.allclose(w.q_groups, expected_weight.q_groups), "q_groups mismatch"


def test_get_multi_weights_row_awq():
    weights = MockWeights(
        [
            "test_get_multi_weights_row_gptq",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "weight"
    quantize = "awq"

    w = weights.get_multi_weights_row(
        prefix=prefix,
        quantize=quantize,
    )

    expected_weight = GPTQWeight(
        qweight=torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]),
        qzeros=torch.tensor([[1.0], [2.0]], dtype=torch.float32),
        scales=torch.tensor([8], dtype=torch.int32),
        g_idx=None,
        bits=torch.tensor([8], dtype=torch.float32),
        groupsize=torch.tensor([4], dtype=torch.float32),
        use_exllama=False,
    )

    assert torch.allclose(w.qweight, expected_weight.qweight), "qweight mismatch"
    assert torch.allclose(w.qzeros, expected_weight.qzeros), "qzeros mismatch"
    assert torch.allclose(w.scales, expected_weight.scales), "scales mismatch"
    assert w.g_idx == expected_weight.g_idx, "g_idx mismatch"
    assert w.bits == expected_weight.bits, "bits mismatch"
    assert w.groupsize == expected_weight.groupsize, "groupsize mismatch"
    assert w.use_exllama == expected_weight.use_exllama, "use_exllama mismatch"


def test_get_multi_weights_row_marlin():
    weights = MockWeights(
        [
            "test_get_multi_weights_row_marlin",
        ],
        device="cpu",
        dtype=torch.float32,
        process_group=dummy_process_group,
        dummy_fs=dummy_file_system,
    )

    prefix = "weight"
    quantize = "marlin"

    w = weights.get_multi_weights_row(
        prefix=prefix,
        quantize=quantize,
    )

    expected_weight = MarlinWeight(
        B=torch.tensor([[1, 2], [3, 4]], dtype=torch.int32),
        s=torch.tensor([0.5], dtype=torch.float16),
    )

    assert torch.allclose(w.B, expected_weight.B), "B mismatch"
    assert torch.allclose(w.s, expected_weight.s), "s mismatch"