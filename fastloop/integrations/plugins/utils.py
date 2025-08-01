import io
import wave


def bytes_to_wav(
    audio_bytes: bytes, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2
) -> io.BytesIO:
    # Create an in-memory WAV file
    wav_buffer = io.BytesIO()

    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)  # mono or stereo
        wav_file.setsampwidth(sample_width)  # 2 bytes = 16-bit
        wav_file.setframerate(sample_rate)  # sample rate
        wav_file.writeframes(audio_bytes)

    wav_buffer.seek(0)
    return wav_buffer
