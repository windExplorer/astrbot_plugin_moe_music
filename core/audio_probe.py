"""按文件头识别音频容器（扩展名不可信）。

背景：插件下载音频时的扩展名是按**请求的音质**推断出来的（`flac` → `.flac`），
但上游不一定真给这种容器——QQ音乐的无损链接实测会回 m4a，音源也可能回一段错误页
或加密文件。扩展名错了有两个后果：

1. 标签写入器用错（mutagen 直接抛 ``FLACNoHeaderError: is not a valid FLAC file``）；
2. 发给用户 / 协议端的文件名与真实格式不符，播放器按错格式解析。

所以「容器以内容为准」：下载完成后按文件头定扩展名，嵌入标签前也再确认一次。
识别不出来时返回 ``None``（调用方保留原扩展名并告警，绝不瞎猜）。
"""

# ID3v2 标签头长度（10 字节：'ID3' + 版本 2 + 标志 1 + synchsafe 长度 4）
_ID3_HEADER_LEN = 10

# Content-Type -> 扩展名（魔数认不出时的兜底）
_CONTENT_TYPE_EXT: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/aacp": ".aac",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/ape": ".ape",
    "audio/x-ape": ".ape",
}


def id3_tag_size(head: bytes) -> int | None:
    """ID3v2 标签总长度（含 10 字节头）；不是 ID3 或长度不可信时返回 None。

    用于「ID3 标签 + FLAC」这种真实存在的畸形组合：标签后面才是 fLaC 魔数。
    """
    if len(head) < _ID3_HEADER_LEN or head[:3] != b"ID3":
        return None
    size = 0
    for byte in head[6:10]:
        if byte & 0x80:  # synchsafe 编码要求每字节最高位为 0，否则视为不可信
            return None
        size = (size << 7) | byte
    if size <= 0:
        return None
    return size + _ID3_HEADER_LEN


def sniff_audio_ext(head: bytes, content_type: str | None = None) -> str | None:
    """按文件头（必要时用 Content-Type 兜底）判断音频真实容器，返回扩展名。

    Args:
        head: 文件开头若干字节（16 字节起步，含 ID3 标签时建议 64 字节以上）。
        content_type: 响应头 Content-Type，仅作魔数认不出时的兜底。

    Returns:
        形如 ``.flac`` / ``.mp3`` / ``.m4a`` 的扩展名；无法识别返回 ``None``。
    """
    if head:
        if head[:4] == b"fLaC":
            return ".flac"
        if head[:4] == b"OggS":
            # Ogg 容器（含 opus 流）：统一按 .ogg 处理，mutagen 会自行分发
            return ".ogg"
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return ".wav"
        if head[:4] == b"FORM" and head[8:12] == b"AIFF":
            return ".aiff"
        if head[:4] == b"MAC ":
            return ".ape"
        if head[:4] == b"wvpk":
            return ".wv"
        if head[4:8] == b"ftyp":
            return ".m4a"
        if head[:4] == b"\x30\x26\xb2\x75":  # ASF/WMA 的 GUID 头
            return ".wma"
        if head[:5] == b"#!AMR":
            return ".amr"
        if head[:3] == b"ID3":
            size = id3_tag_size(head)
            if size is not None and head[size : size + 4] == b"fLaC":
                return ".flac"
            return ".mp3"
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return ".mp3"  # MPEG 音频帧同步字（11 位全 1）

    ctype = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(ctype)
