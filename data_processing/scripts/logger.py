import logging
from abc import abstractmethod, ABC
from typing import Union
from rich.logging import RichHandler
import inspect

_level_to_sign = {
    "info": "ℹ️",
    "debug": "🔎",
    "warning": "⚠️",
    "error": "❌",
    "critical": "💣",
}

not_found_sign = "❗️❓"

class BaseLogger(ABC):
    def __init__(self, filename: Union[str,None] = None):
        if filename is None:
            self._start_stream_logger()
        else:
            self._start_file_logger(filename)

    @abstractmethod
    def _start_file_logger(self, filename):
        pass

    @abstractmethod
    def _start_stream_logger(self):
        pass

    def add_to_log(self, s, level="info"):
        if level not in _level_to_sign:
            raise ValueError("Unrecognized level value: {}. Must be one of: {}".format(level, _level_to_sign))
        # append the file name and line number of the caller
        caller_frame = inspect.getframeinfo(inspect.currentframe().f_back)
        caller_filename = caller_frame.filename.split("/")[-1]
        message = _level_to_sign[level] + "  " + s + " ({}:{})".format(caller_filename, caller_frame.lineno)
        if level == "info":
            self.logger.info(message)
        elif level == "debug":
            self.logger.debug(message)
        elif level == "error":
            self.logger.error(message)
        elif level == "warning":
            self.logger.warning(message)
        elif level == "critical":
            self.logger.critical(message)

class SimpleLogger(BaseLogger):
    def __init__(self, filename: Union[str,None] = None):
        super().__init__(filename)

    def _start_file_logger(self, filename):
        for handler in logging.root.handlers:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level = logging.NOTSET,
            format = "%(asctime)s %(levelname)-8s %(message)s",
            datefmt = "[%Y-%m-%d %H:%M:%S]",
            filename = filename,
        )
        self.logger = logging.getLogger()

    def _start_stream_logger(self):
        raise NotImplementedError


class RichLogger(BaseLogger):
    def __init__(self, filename: Union[str,None] = None):
        super().__init__(filename)

    def _start_file_logger(self, filename):
        raise NotImplementedError

    def _start_stream_logger(self):
        logging.basicConfig(
            level=logging.NOTSET,
            format="%(message)s",
            datefmt="[%Y-%m-%d %H:%M:%S]",
            handlers=[RichHandler(omit_repeated_times=False)],
        )
        self.logger = logging.getLogger("rich")