class N2SQLError(Exception):
    """智能问数内部错误；code 用于 API / 聊天节点映射错误语义。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
