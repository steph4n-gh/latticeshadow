#include <iostream>
#include <random>
#include <vector>
#include <cmath>
#include <stdint.h>

extern "C" {

// LWE parameters:
// n = 512
// q = 2^32 (implicit in uint32_t arithmetic)
// scale = 65536 (2^16)

void generate_key(uint32_t* secret_key, int n) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<int> dist(0, 1);
    for (int i = 0; i < n; ++i) {
        secret_key[i] = dist(gen);
    }
}

void encrypt_bit(uint32_t* secret_key, int n, uint32_t bit, uint32_t* a_out, uint32_t* b_out) {
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<uint32_t> dist_a(0, 0xFFFFFFFF);
    
    // Discrete Gaussian noise (using normal distribution and rounding)
    std::normal_distribution<double> dist_e(0.0, 4.0); 

    // Generate random vector a
    uint32_t dot_product = 0;
    for (int i = 0; i < n; ++i) {
        a_out[i] = dist_a(gen);
        dot_product += a_out[i] * secret_key[i];
    }

    int32_t noise = std::round(dist_e(gen));
    uint32_t scaled_message = bit * 65536; // scale is 2^16

    *b_out = dot_product + noise + scaled_message;
}

uint32_t decrypt_distance(uint32_t* secret_key, int n, uint32_t* a_sum, uint32_t b_sum) {
    uint32_t dot_product = 0;
    for (int i = 0; i < n; ++i) {
        dot_product += a_sum[i] * secret_key[i];
    }
    
    // b_sum - dot_product = noise + distance * scale
    int32_t diff = (int32_t)(b_sum - dot_product);
    
    // Round to the nearest multiple of 65536
    double val = (double)diff / 65536.0;
    
    // Handle integer overflow/wrap-around
    int32_t rounded = std::round(val);
    if (rounded < 0) {
        rounded = 0;
    }
    return (uint32_t)rounded;
}

void homomorphic_xor(uint32_t* a_in, uint32_t b_in, uint32_t public_bit, uint32_t* a_out, uint32_t* b_out, int n) {
    if (public_bit == 0) {
        for (int i = 0; i < n; ++i) {
            a_out[i] = a_in[i];
        }
        *b_out = b_in;
    } else {
        for (int i = 0; i < n; ++i) {
            a_out[i] = -a_in[i];
        }
        *b_out = 65536 - b_in;
    }
}

}
