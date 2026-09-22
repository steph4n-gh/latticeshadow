import sqlite3

import pytest
import torch
from fastapi.testclient import TestClient

from latticeshadow_db.cloud_server import CONFIG, app
from latticeshadow_db.latticedb import Collection, PrivacyEngine
from latticeshadow_db.latticedb.distiller import AutoDistiller
from latticeshadow_db.latticedb.drosophila import DrosophilaHasher, MAX_FLYHASH_INPUT_DIM
from latticeshadow_db.latticedb.holographic import (
    MAX_HOLOGRAPHIC_DIM,
    circular_convolution,
    generate_key_vector,
)
from latticeshadow_db.mitm import StickyTLSProxy
from latticeshadow_db.quant import LeechLatticeQuantizer


@pytest.fixture
def cloud_config():
    original = CONFIG.copy()
    try:
        yield CONFIG
    finally:
        CONFIG.clear()
        CONFIG.update(original)


def test_cloud_server_requires_configured_api_key(cloud_config):
    cloud_config.update(
        api_key=None,
        local_dim=2,
        cloud_dim=2,
        device="cpu",
        max_batch_rows=2,
        max_total_floats=4,
    )
    client = TestClient(app)

    response = client.post("/v1/align", json={"obfuscated_state": [[1.0, 2.0]]})

    assert response.status_code == 503


def test_cloud_server_rejects_default_secret_and_oversized_payload(cloud_config):
    cloud_config.update(
        api_key="real-secret",
        local_dim=2,
        cloud_dim=2,
        device="cpu",
        max_batch_rows=1,
        max_total_floats=2,
    )
    client = TestClient(app)

    wrong_token = client.post(
        "/v1/align",
        json={"obfuscated_state": [[1.0, 2.0]]},
        headers={"Authorization": "Bearer default-secret-key"},
    )
    too_large = client.post(
        "/v1/align",
        json={"obfuscated_state": [[1.0, 2.0], [3.0, 4.0]]},
        headers={"Authorization": "Bearer real-secret"},
    )

    assert wrong_token.status_code == 401
    assert too_large.status_code == 413


def test_document_encryption_survives_rotation_and_is_not_master_derived(tmp_path):
    db_path = str(tmp_path / "privacy.sqlite")
    vectors = {
        "alpha": torch.tensor([1.0, 0.0, 0.0, 0.0]),
        "beta": torch.tensor([0.0, 1.0, 0.0, 0.0]),
    }

    def embed(text: str) -> torch.Tensor:
        return vectors[text]

    collection = Collection(
        "secure_docs",
        db_path=db_path,
        embedding_fn=embed,
        embedding_dim=4,
        privacy_enabled=True,
        master_key="old-master",
    )
    collection.add(["alpha"], ids=["doc_alpha"])
    with sqlite3.connect(db_path) as conn:
        stored_document = conn.execute(
            "SELECT document FROM vectors WHERE collection = ?",
            ("secure_docs",),
        ).fetchone()[0]

    assert stored_document.startswith("enc:v2:")
    assert PrivacyEngine(dim=4, master_key="old-master").decrypt_document(stored_document) == stored_document

    collection.rotate_master_key("new-master")
    reloaded = Collection(
        "secure_docs",
        db_path=db_path,
        embedding_fn=embed,
        embedding_dim=4,
        privacy_enabled=True,
        master_key="new-master",
    )

    assert reloaded.search("alpha", n_results=1).documents == ["alpha"]
    with pytest.raises(PermissionError):
        Collection(
            "secure_docs",
            db_path=db_path,
            embedding_fn=embed,
            embedding_dim=4,
            privacy_enabled=True,
            master_key="old-master",
        )

    reloaded.crypto_shred()
    assert PrivacyEngine(dim=4, master_key="new-master").decrypt_document(stored_document) == stored_document


def test_privacy_auto_distill_encrypts_persisted_pairs(tmp_path):
    db_path = str(tmp_path / "distill.sqlite")

    def embed(text: str) -> torch.Tensor:
        return torch.tensor([1.0, 0.0, 0.0, 0.0])

    def cloud(text: str) -> torch.Tensor:
        return torch.tensor([0.0, 1.0, 0.0, 0.0])

    collection = Collection(
        "distill_secure",
        db_path=db_path,
        embedding_fn=embed,
        cloud_fn=cloud,
        embedding_dim=4,
        privacy_enabled=True,
        auto_distill=True,
        master_key="distill-master",
    )
    collection.add(["alpha"], ids=["doc_alpha"])

    with sqlite3.connect(db_path) as conn:
        local_blob, cloud_blob = conn.execute(
            "SELECT local_blob, cloud_blob FROM distillation_pairs WHERE collection = ?",
            ("distill_secure",),
        ).fetchone()

    assert local_blob.startswith(b"enc1:")
    assert cloud_blob.startswith(b"enc1:")
    assert b"NUMPY" not in local_blob
    assert b"NUMPY" not in cloud_blob
    assert collection._distiller.train(epochs=1) < float("inf")

    plaintext_reader = AutoDistiller(
        dim=4,
        db_path=db_path,
        collection="distill_secure",
        min_samples=100,
    )
    with pytest.raises(PermissionError):
        plaintext_reader.train(epochs=1)


def test_bounded_compute_surfaces_reject_oversized_workloads():
    with pytest.raises(ValueError, match="FlyHash input_dim"):
        DrosophilaHasher(input_dim=MAX_FLYHASH_INPUT_DIM + 1)

    with pytest.raises(ValueError, match="Leech lattice quantization"):
        LeechLatticeQuantizer(max_blocks=1).quantize(torch.zeros(2, 24))

    with pytest.raises(ValueError, match="Holographic dimension"):
        circular_convolution(
            torch.zeros(MAX_HOLOGRAPHIC_DIM + 1),
            torch.zeros(MAX_HOLOGRAPHIC_DIM + 1),
        )

    with pytest.raises(ValueError, match="Holographic dimension"):
        generate_key_vector("doc", MAX_HOLOGRAPHIC_DIM + 1)


def test_mitm_cert_paths_are_private_and_symlinks_are_rejected(tmp_path):
    proxy = StickyTLSProxy(cert_dir=tmp_path / "certs")

    assert proxy.cert_file != "/tmp/shadow_mitm_cert.pem"
    assert proxy.key_file != "/tmp/shadow_mitm_key.pem"
    assert str(tmp_path / "certs") in proxy.cert_file

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    symlink_dir = tmp_path / "link"
    symlink_dir.symlink_to(target_dir)
    with pytest.raises(PermissionError, match="symlink"):
        StickyTLSProxy(cert_dir=symlink_dir)._ensure_private_cert_dir()

    broad_dir = tmp_path / "broad"
    broad_dir.mkdir()
    broad_dir.chmod(0o755)
    with pytest.raises(PermissionError, match="group/other"):
        StickyTLSProxy(cert_dir=broad_dir)._ensure_private_cert_dir()

    proxy.cert_dir.mkdir(mode=0o700, parents=True)
    target_file = proxy.cert_dir / "seeded.pem"
    target_file.write_text("seeded")
    cert_link = proxy.cert_dir / "shadow_mitm_cert.pem"
    cert_link.symlink_to(target_file)
    with pytest.raises(PermissionError, match="symlink"):
        proxy._trust_cert_macos()
