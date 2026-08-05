"""
AIena Signals — package per elaborazione segnalazioni come seed investigativi.
"""
from .entity_extractor import EntityExtractor
from .aiena_signal_processor import SignalProcessor

__all__ = ["EntityExtractor", "SignalProcessor"]
