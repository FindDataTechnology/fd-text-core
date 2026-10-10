"""大正文冷存：zstd 压缩块 + 对象存储指针，PG 只存 key。

自有格式 FDTEXTBLOCK1（魔数 + 版本 + u32-BE 原始长度 + zstd 帧）。
为什么不继承 wenshu 的既有块格式：那些 reader 把两个历史写者的 magic
硬编码在查询路径里成了债；本引擎只用一种自有格式，读写器都在仓内。
"""
from __future__ import annotations

import struct

import zstandard as zstd

MAGIC = b"FDTEXTBLOCK1"
_HEADER = struct.Struct(">12sB I")  # magic(12) + version(1) + u32-BE 原始长度

COMPRESS_LEVEL = 3


class BlockFormatError(RuntimeError):
    """块格式非法（魔数/版本/长度不符）。"""


def _s3():
    """懒构造 boto3 客户端（配置来自环境变量）。"""
    import boto3
    from botocore.config import Config

    from .config import Settings

    s = Settings.from_env()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=Config(connect_timeout=6, read_timeout=30, signature_version="s3v4"),
        region_name="us-east-1",
    ), s.s3_bucket


def encode_block(text: str) -> bytes:
    """文本 -> FDTEXTBLOCK1 字节串。"""
    raw = text.encode("utf-8")
    compressed = zstd.ZstdCompressor(level=COMPRESS_LEVEL).compress(raw)
    return _HEADER.pack(MAGIC, 1, len(raw)) + compressed


def decode_block(blob: bytes, *, max_chars: int = 200_000) -> str:
    """FDTEXTBLOCK1 字节串 -> 文本（按 max_chars 截断返回）。"""
    if len(blob) < _HEADER.size:
        raise BlockFormatError(f"块过短: {len(blob)} 字节")
    magic, version, raw_len = _HEADER.unpack(blob[: _HEADER.size])
    if magic != MAGIC:
        raise BlockFormatError(f"块魔数不符: {magic!r}")
    if version != 1:
        raise BlockFormatError(f"不支持的块版本: {version}")
    raw = zstd.ZstdDecompressor().decompress(blob[_HEADER.size :], max_output_size=raw_len)
    if len(raw) != raw_len:
        raise BlockFormatError(f"解压长度不符: header={raw_len} actual={len(raw)}")
    text = raw.decode("utf-8")
    return text[:max_chars]


def put_block(domain: str, key_suffix: str, text: str) -> tuple[str, int]:
    """写冷存，返回 (对象键, 压缩后字节数)。"""
    client, bucket = _s3()
    key = f"{domain}/blocks/{key_suffix}"
    blob = encode_block(text)
    client.put_object(Bucket=bucket, Key=key, Body=blob)
    return key, len(blob)


def get_block(key: str, *, max_chars: int = 200_000) -> str:
    """读冷存。对象缺失/格式坏时抛错，不静默返回空串。"""
    client, bucket = _s3()
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        blob = resp["Body"].read()
    except Exception as exc:
        raise RuntimeError(f"冷存读取失败 key={key}: {type(exc).__name__}: {exc}") from exc
    return decode_block(blob, max_chars=max_chars)


def roundtrip_ok(text: str) -> bool:
    """自检用：编解码往返一致。"""
    return decode_block(encode_block(text), max_chars=len(text) + 1) == text
