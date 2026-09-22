import os
import sys
import ctypes
import numpy as np
from typing import Tuple

# Load the compiled shared library
lib_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(lib_dir, "liblwe.dylib")

# Fallback to .so or other names if needed, but we compiled as liblwe.dylib
if not os.path.exists(lib_path):
    raise FileNotFoundError(f"LWE shared library not found at {lib_path}. Run compilation first.")

lib = ctypes.CDLL(lib_path)

# Declare argument and return types
# void generate_key(uint32_t* secret_key, int n)
lib.generate_key.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
lib.generate_key.restype = None

# void encrypt_bit(uint32_t* secret_key, int n, uint32_t bit, uint32_t* a_out, uint32_t* b_out)
lib.encrypt_bit.argtypes = [
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_int,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint32)
]
lib.encrypt_bit.restype = None

# uint32_t decrypt_distance(uint32_t* secret_key, int n, uint32_t* a_sum, uint32_t b_sum)
lib.decrypt_distance.argtypes = [
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_uint32
]
lib.decrypt_distance.restype = ctypes.c_uint32

# void homomorphic_xor(uint32_t* a_in, uint32_t b_in, uint32_t public_bit, uint32_t* a_out, uint32_t* b_out, int n)
lib.homomorphic_xor.argtypes = [
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_int
]
lib.homomorphic_xor.restype = None

LWE_N = 512

def generate_key() -> np.ndarray:
    """Generate a random LWE secret key of dimension 512."""
    key = np.zeros(LWE_N, dtype=np.uint32)
    lib.generate_key(key.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)), LWE_N)
    return key

def encrypt_bitmask(key: np.ndarray, bitmask: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Encrypt a binary bitmask vector (e.g. 128 bits) bit-by-bit.
    Returns:
        enc_a: Shape (len(bitmask), LWE_N) - uint32 matrix
        enc_b: Shape (len(bitmask),) - uint32 vector
    """
    num_bits = len(bitmask)
    enc_a = np.zeros((num_bits, LWE_N), dtype=np.uint32)
    enc_b = np.zeros(num_bits, dtype=np.uint32)
    
    key_ptr = key.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32))
    
    for i in range(num_bits):
        a_out = np.zeros(LWE_N, dtype=np.uint32)
        b_out = ctypes.c_uint32(0)
        
        lib.encrypt_bit(
            key_ptr,
            LWE_N,
            int(bitmask[i]),
            a_out.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            ctypes.byref(b_out)
        )
        enc_a[i] = a_out
        enc_b[i] = b_out.value
        
    return enc_a, enc_b

def decrypt_distance(key: np.ndarray, enc_a_sum: np.ndarray, enc_b_sum: int) -> int:
    """Decrypt the homomorphically computed Hamming distance."""
    key_ptr = key.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32))
    a_ptr = enc_a_sum.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32))
    return int(lib.decrypt_distance(key_ptr, LWE_N, a_ptr, ctypes.c_uint32(enc_b_sum)))

def evaluate_distance_homomorphically(
    enc_a: np.ndarray, 
    enc_b: np.ndarray, 
    public_bitmask: np.ndarray
) -> Tuple[np.ndarray, int]:
    """
    Evaluate the Hamming distance between encrypted bitmask and public_bitmask homomorphically.
    Returns:
        a_sum: Vector of length LWE_N
        b_sum: Scalar integer sum
    """
    num_bits = len(public_bitmask)
    if num_bits != len(enc_b):
        raise ValueError("Bitmask lengths do not match encrypted query dimension.")
        
    a_sum = np.zeros(LWE_N, dtype=np.uint32)
    b_sum = 0
    
    for i in range(num_bits):
        a_in = enc_a[i]
        b_in = enc_b[i]
        public_bit = int(public_bitmask[i])
        
        a_out = np.zeros(LWE_N, dtype=np.uint32)
        b_out = ctypes.c_uint32(0)
        
        lib.homomorphic_xor(
            a_in.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            ctypes.c_uint32(b_in),
            public_bit,
            a_out.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
            ctypes.byref(b_out),
            LWE_N
        )
        # Sum up vectors and scalars to calculate homomorphic distance
        a_sum += a_out
        b_sum = (b_sum + b_out.value) & 0xFFFFFFFF
        
    return a_sum, b_sum
