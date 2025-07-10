class InvalidConfigError(Exception):
    pass


class LoopNotFoundError(Exception):
    pass


class LoopClaimError(Exception):
    pass


class LoopPausedError(Exception):
    pass


class LoopStoppedError(Exception):
    pass


class LoopAlreadyDefinedError(Exception):
    pass


class EventTimeoutError(Exception):
    pass
