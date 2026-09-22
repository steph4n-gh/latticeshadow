import ctypes
import logging

logger = logging.getLogger("latticeshadow.security")

# Load macOS Frameworks
cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
libc = ctypes.CDLL(None)

# Bind CoreFoundation functions
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

cf.CFNumberCreate.restype = ctypes.c_void_p
cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

cf.CFDataCreate.restype = ctypes.c_void_p
cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]

cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]

cf.CFDataGetLength.restype = ctypes.c_long
cf.CFDataGetLength.argtypes = [ctypes.c_void_p]

cf.CFDictionaryCreate.restype = ctypes.c_void_p
cf.CFDictionaryCreate.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.c_void_p
]

# Bind Security APIs
sec.SecKeyCreateRandomKey.restype = ctypes.c_void_p
sec.SecKeyCreateRandomKey.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

sec.SecKeyCopyPublicKey.restype = ctypes.c_void_p
sec.SecKeyCopyPublicKey.argtypes = [ctypes.c_void_p]

sec.SecKeyCreateEncryptedData.restype = ctypes.c_void_p
sec.SecKeyCreateEncryptedData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

sec.SecKeyCreateDecryptedData.restype = ctypes.c_void_p
sec.SecKeyCreateDecryptedData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

sec.SecItemCopyMatching.restype = ctypes.c_int32
sec.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

sec.SecItemDelete.restype = ctypes.c_int32
sec.SecItemDelete.argtypes = [ctypes.c_void_p]

# Load CoreFoundation Constants
kCFTypeDictionaryKeyCallBacks = ctypes.c_void_p.in_dll(cf, 'kCFTypeDictionaryKeyCallBacks')
kCFTypeDictionaryValueCallBacks = ctypes.c_void_p.in_dll(cf, 'kCFTypeDictionaryValueCallBacks')
kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, 'kCFBooleanTrue')
kCFBooleanFalse = ctypes.c_void_p.in_dll(cf, 'kCFBooleanFalse')

# Security Constants
LABEL_PREFIX = "com.latticeshadow."
ALGORITHM = "algid:encrypt:ECIES:ECDH:KDFX963:SHA256:AESGCM"

def cfstr(s):
    return cf.CFStringCreateWithCString(None, s.encode('utf-8'), 0x08000100)

def cfnum(n):
    return cf.CFNumberCreate(None, 3, ctypes.byref(ctypes.c_int(n))) # 3 = kCFNumberSInt32Type

def make_cf_dict(py_dict):
    keys = (ctypes.c_void_p * len(py_dict))()
    values = (ctypes.c_void_p * len(py_dict))()
    for i, (k, v) in enumerate(py_dict.items()):
        keys[i] = cfstr(k)
        if isinstance(v, bool):
            values[i] = kCFBooleanTrue if v else kCFBooleanFalse
        elif isinstance(v, int):
            values[i] = cfnum(v)
        elif isinstance(v, str):
            values[i] = cfstr(v)
        elif isinstance(v, dict):
            values[i] = make_cf_dict(v)
        else:
            values[i] = v
    return cf.CFDictionaryCreate(
        None,
        keys,
        values,
        len(py_dict),
        ctypes.byref(kCFTypeDictionaryKeyCallBacks),
        ctypes.byref(kCFTypeDictionaryValueCallBacks)
    )

def cfdata_to_bytes(cf_data):
    if not cf_data:
        return b""
    length = cf.CFDataGetLength(cf_data)
    ptr = cf.CFDataGetBytePtr(cf_data)
    return bytes(ptr[:length])

def shield_process():
    """Prevent debugger attachment (lldb/gdb) to protect RAM keys."""
    try:
        # PT_DENY_ATTACH is 31 on macOS
        res = libc.ptrace(31, 0, 0, 0)
        if res == 0:
            logger.info("Process memory shield active (PT_DENY_ATTACH).")
        return res == 0
    except Exception as e:
        logger.warning(f"Failed to enable process memory shield: {e}")
        return False

def lock_buffer(buf: bytearray):
    """Lock memory pages to prevent key material swapping to disk."""
    try:
        addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))
        res = libc.mlock(addr, len(buf))
        if res == 0:
            logger.debug(f"Memory locked successfully for buffer size {len(buf)}.")
        return res == 0
    except Exception as e:
        logger.warning(f"Failed to lock memory: {e}")
        return False

def unlock_buffer(buf: bytearray):
    """Unlock memory pages."""
    try:
        addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))
        res = libc.munlock(addr, len(buf))
        return res == 0
    except Exception as e:
        logger.warning(f"Failed to unlock memory: {e}")
        return False

def _delete_key(label: str):
    query = {
        "class": "keys",
        "labl": label
    }
    sec.SecItemDelete(make_cf_dict(query))

def generate_secure_enclave_key(label: str) -> bool:
    """
    Generates an EC P-256 key pair permanently in the Secure Enclave.
    Falls back to software-based Keychain if the Secure Enclave is unavailable.
    """
    full_label = LABEL_PREFIX + label
    _delete_key(full_label)
    
    # 1. Try Secure Enclave first
    parameters = {
        "type": "73",  # kSecAttrKeyTypeECSECPrimeRandom
        "bsiz": 256,   # kSecAttrKeySizeInBits
        "tkid": "com.apple.setoken",  # kSecAttrTokenIDSecureEnclave
        "private": {
            "perm": True,  # kSecAttrIsPermanent
            "labl": full_label
        },
        "public": {
            "perm": True,
            "labl": full_label + ".pub"
        }
    }
    
    error = ctypes.c_void_p()
    key_ref = sec.SecKeyCreateRandomKey(make_cf_dict(parameters), ctypes.byref(error))
    if key_ref:
        logger.info(f"Successfully generated Secure Enclave key pair: {full_label}")
        return True
        
    # 2. Fallback to standard Keychain-backed software key pair
    logger.warning("Secure Enclave key generation failed. Falling back to software Keychain...")
    parameters.pop("tkid")
    
    error = ctypes.c_void_p()
    key_ref = sec.SecKeyCreateRandomKey(make_cf_dict(parameters), ctypes.byref(error))
    if key_ref:
        logger.info(f"Successfully generated software Keychain key pair: {full_label}")
        return True
        
    logger.error("Failed to generate software Keychain key pair.")
    return False

def _get_private_key(label: str):
    """Retrieve key reference via ctypes."""
    full_label = LABEL_PREFIX + label
    query = {
        "class": "keys",
        "labl": full_label,
        "r_Ref": True,
        "m_Limit": "m_LimitOne"
    }
    
    result_ptr = ctypes.c_void_p()
    status = sec.SecItemCopyMatching(make_cf_dict(query), ctypes.byref(result_ptr))
    if status == 0 and result_ptr.value:
        return result_ptr.value
    return None

def encrypt_with_secure_enclave(label: str, plaintext: bytes) -> bytes:
    """Encrypt a payload using the Secure Enclave key pair's public key."""
    priv_key = _get_private_key(label)
    if not priv_key:
        if not generate_secure_enclave_key(label):
            raise RuntimeError("Failed to resolve or generate Secure Enclave key.")
        priv_key = _get_private_key(label)
        if not priv_key:
            raise RuntimeError("Failed to retrieve newly generated private key.")
            
    pub_key = sec.SecKeyCopyPublicKey(priv_key)
    if not pub_key:
        raise RuntimeError("Failed to extract public key from Enclave key.")
        
    plaintext_data = cf.CFDataCreate(None, plaintext, len(plaintext))
    alg_str = cfstr(ALGORITHM)
    
    error = ctypes.c_void_p()
    cipher_cf = sec.SecKeyCreateEncryptedData(
        pub_key,
        alg_str,
        plaintext_data,
        ctypes.byref(error)
    )
    if not cipher_cf:
        raise RuntimeError(f"Enclave encryption failed: {error.value}")
        
    return cfdata_to_bytes(cipher_cf)

def decrypt_with_secure_enclave(label: str, ciphertext: bytes) -> bytes:
    """Decrypt a payload using the Secure Enclave private key."""
    priv_key = _get_private_key(label)
    if not priv_key:
        raise RuntimeError(f"Private key not found for label: {label}")
        
    ciphertext_data = cf.CFDataCreate(None, ciphertext, len(ciphertext))
    alg_str = cfstr(ALGORITHM)
    
    error = ctypes.c_void_p()
    plain_cf = sec.SecKeyCreateDecryptedData(
        priv_key,
        alg_str,
        ciphertext_data,
        ctypes.byref(error)
    )
    if not plain_cf:
        raise RuntimeError(f"Enclave decryption failed: {error.value}")
        
    return cfdata_to_bytes(plain_cf)
