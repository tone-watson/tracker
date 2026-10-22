class TrackerError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status
