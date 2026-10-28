// ldpc.h -- the LDPC code's C interface (ldpc.cpp): AVM's rate-1/2 QC-LDPC,
// 648/1296/1944-bit blocks chosen by message length.
#pragma once
#include <cstdint>

extern "C" {
// coded bits for msg_bits message bits (whole blocks)
long long ldpc_coded_len(long long msg_bits);
// systematic encode: out gets ldpc_coded_len(msg_bits) bits
void ldpc_encode(const uint8_t* bits, long long msg_bits, uint8_t* out);
// layered normalised min-sum decode (positive LLR = likely 0): out_bits gets
// msg_bits bits, out_cw (may be null) the decoded codewords; 1 if every block
// converged, 0 if not, -1 if llr is too short; *iters (may be null) the total
// iterations
int ldpc_decode(const double* llr, long long n_llr, long long msg_bits, int max_iter, double alpha,
                int stall_iter, uint8_t* out_bits, uint8_t* out_cw, long long* iters);
}
