from aiogram.fsm.state import State, StatesGroup


class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirming = State()
