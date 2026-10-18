"""Native PyTorch SDPA baseline for the attention output tensor only.

Does not manufacture the Flash interface's four auxiliary return values.
"""
import torch.nn.functional as F


def baseline(query, key, value, cumulative_sequence_length_q,
             cumulative_sequence_length_k, max_q, max_k, dropout_p,
             is_causal, return_debug_mask, *, scale=None, window_size_left=None,
             window_size_right=None, seqused_k=None, alibi_slopes=None):
    if (any(v is not None for v in [cumulative_sequence_length_q,
            cumulative_sequence_length_k, window_size_left, window_size_right,
            seqused_k, alibi_slopes]) or dropout_p != 0 or return_debug_mask
            or max_q != query.shape[1] or max_k != key.shape[1]):
        raise ValueError('SDPA baseline supports fixed-length, no-dropout/no-window attention only')
    return F.scaled_dot_product_attention(
        query.transpose(1, 2), key.transpose(1, 2), value.transpose(1, 2),
        dropout_p=0.0, is_causal=is_causal, scale=scale).transpose(1, 2).contiguous()


if __name__ == '__main__':
    from runner import main
    raise SystemExit(main('_flash_attention_forward', baseline, 'attention_math_v1', partial=True))
