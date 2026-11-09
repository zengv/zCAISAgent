from functools import lru_cache

from zcaisagent.container import Container, build_container


@lru_cache(maxsize=1)
def get_container() -> Container:
    return build_container()
