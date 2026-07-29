"""SongDNA: declarative musical skeletons with deterministic exports."""

from .compiler import CompileResult, compile_song
from .errors import SongDNAError, ValidationError

__all__ = ["CompileResult", "SongDNAError", "ValidationError", "compile_song"]

__version__ = "0.1.0"

