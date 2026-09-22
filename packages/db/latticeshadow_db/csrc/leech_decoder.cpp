#include <torch/extension.h>
#include <vector>
#include <cmath>
#include <iostream>

// Parity matrix B for the [24, 12, 8] extended binary Golay code G_24.
// G = [I_12 | B]
static const int B[12][12] = {
    {1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1},
    {1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0},
    {0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 1},
    {1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1},
    {1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1},
    {1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 0},
    {0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0, 0},
    {0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 0},
    {0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1},
    {1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0},
    {0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1},
    {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0}
};

// Generates the 4096 codewords of G_24
// Pre-calculating them once makes the decoder extremely fast.
static std::vector<std::vector<int>> generate_golay_codewords() {
    std::vector<std::vector<int>> codewords(4096, std::vector<int>(24, 0));
    for (int u = 0; u < 4096; ++u) {
        // First 12 bits are u
        for (int i = 0; i < 12; ++i) {
            codewords[u][i] = (u >> i) & 1;
        }
        // Last 12 bits are u * B mod 2
        for (int j = 0; j < 12; ++j) {
            int sum = 0;
            for (int i = 0; i < 12; ++i) {
                sum += codewords[u][i] * B[i][j];
            }
            codewords[u][12 + j] = sum % 2;
        }
    }
    return codewords;
}

// Global cache of codewords to avoid regeneration
static const std::vector<std::vector<int>> G_codewords = generate_golay_codewords();

// Decodes a single 24D vector y' = y * sqrt(8) to the nearest vector in v in sqrt(8)*Lambda_24
// Returns a std::vector<double> of size 24.
static std::vector<double> decode_single_vector(const float* y_ptr) {
    double best_dist_sq = 1e30;
    std::vector<double> best_v(24, 0.0);

    // Temp storage
    double v_cand[24];

    for (int u = 0; u < 4096; ++u) {
        const auto& g = G_codewords[u];

        // --- Case A: Even coordinates ---
        {
            double sum_v = 0.0;
            for (int i = 0; i < 24; ++i) {
                double yi = y_ptr[i];
                if (g[i] == 0) {
                    v_cand[i] = 4.0 * std::round(yi / 4.0);
                } else {
                    v_cand[i] = 4.0 * std::round((yi - 2.0) / 4.0) + 2.0;
                }
                sum_v += v_cand[i];
            }

            int sum_v_int = static_cast<int>(std::round(sum_v));
            if ((sum_v_int % 8 + 8) % 8 == 4) {
                // Find index i maximizing |y_i - v_cand,i| to apply parity correction
                int best_idx = 0;
                double max_err = -1.0;
                for (int i = 0; i < 24; ++i) {
                    double err = std::abs(y_ptr[i] - v_cand[i]);
                    if (err > max_err) {
                        max_err = err;
                        best_idx = i;
                    }
                }
                // Apply +/- 4 correction to change sum mod 8
                double diff = y_ptr[best_idx] - v_cand[best_idx];
                double sign = (diff >= 0.0) ? 1.0 : -1.0;
                v_cand[best_idx] += 4.0 * sign;
            }

            // Compute squared distance
            double dist_sq = 0.0;
            for (int i = 0; i < 24; ++i) {
                double diff = y_ptr[i] - v_cand[i];
                dist_sq += diff * diff;
            }

            if (dist_sq < best_dist_sq) {
                best_dist_sq = dist_sq;
                for (int i = 0; i < 24; ++i) best_v[i] = v_cand[i];
            }
        }

        // --- Case B: Odd coordinates ---
        {
            double sum_v = 0.0;
            for (int i = 0; i < 24; ++i) {
                double yi = y_ptr[i];
                if (g[i] == 0) {
                    v_cand[i] = 4.0 * std::round((yi - 1.0) / 4.0) + 1.0;
                } else {
                    v_cand[i] = 4.0 * std::round((yi - 3.0) / 4.0) + 3.0;
                }
                sum_v += v_cand[i];
            }

            int sum_v_int = static_cast<int>(std::round(sum_v));
            if ((sum_v_int % 8 + 8) % 8 == 0) {
                // Find index i maximizing |y_i - v_cand,i|
                int best_idx = 0;
                double max_err = -1.0;
                for (int i = 0; i < 24; ++i) {
                    double err = std::abs(y_ptr[i] - v_cand[i]);
                    if (err > max_err) {
                        max_err = err;
                        best_idx = i;
                    }
                }
                // Apply +/- 4 correction
                double diff = y_ptr[best_idx] - v_cand[best_idx];
                double sign = (diff >= 0.0) ? 1.0 : -1.0;
                v_cand[best_idx] += 4.0 * sign;
            }

            // Compute squared distance
            double dist_sq = 0.0;
            for (int i = 0; i < 24; ++i) {
                double diff = y_ptr[i] - v_cand[i];
                dist_sq += diff * diff;
            }

            if (dist_sq < best_dist_sq) {
                best_dist_sq = dist_sq;
                for (int i = 0; i < 24; ++i) best_v[i] = v_cand[i];
            }
        }
    }

    return best_v;
}

// PyTorch bindings entry point
// Input: Tensor y of shape (N, 24), float type.
// Output: Tensor of shape (N, 24), float type.
torch::Tensor decode_leech_cpp(torch::Tensor y) {
    TORCH_CHECK(y.is_contiguous(), "Input tensor must be contiguous");
    TORCH_CHECK(y.dim() == 2, "Input tensor must be 2D of shape (N, 24)");
    TORCH_CHECK(y.size(1) == 24, "Input tensor must have exactly 24 columns");

    int N = y.size(0);
    auto result = torch::zeros_like(y);

    // Get accessor or raw pointers
    const float* y_data = y.data_ptr<float>();
    float* r_data = result.data_ptr<float>();

    // Run decoder on each 24D vector
    // Using OpenMP parallel for to run extremely fast on multi-core systems
    #pragma omp parallel for
    for (int n = 0; n < N; ++n) {
        const float* y_vec = y_data + n * 24;
        std::vector<double> v_decoded = decode_single_vector(y_vec);
        float* r_vec = r_data + n * 24;
        for (int i = 0; i < 24; ++i) {
            r_vec[i] = static_cast<float>(v_decoded[i]);
        }
    }

    return result;
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("decode_leech", &decode_leech_cpp, "Leech Lattice (Λ24) fast quantizer/decoder");
}
