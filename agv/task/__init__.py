# -*- coding: utf-8 -*-
"""선반 작업: 끝 노드(선반 앞 스퍼 끝)에 선 뒤 칸을 보고 물건을 집거나 놓는다.

- model.py: 안쪽에서 쓰는 작업·관측·결과 객체(메시지 필드 이름을 모른다)
- decide.py: 관측과 들고 있는 것으로 할 일을 정하는 순수 함수
- executor.py: 관측 자세 -> 칸 보기 -> 팔 -> 다시 보기 -> 주행 자세를 프레임마다 조금씩 진행
- arm_driver.py: 팔 인터페이스(start/poll)와 실물·없는 팔 구현

서버 메시지(JSON 필드)와의 변환은 agv/link/task_msgs.py 한 곳에서만 한다.
Python 3.6 호환.
"""
from .model import Task, SlotView, ShelfView, TaskOutcome, ACTIONS  # noqa: F401
from .decide import decide, merge_views, view_from_perception  # noqa: F401
from .executor import TaskExecutor  # noqa: F401
from .arm_driver import ArmDriver, NullArm, ThreadedArm  # noqa: F401
