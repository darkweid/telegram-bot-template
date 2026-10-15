from aiogram.fsm.state import State, StatesGroup


class NoteStates(StatesGroup):
    waiting_text = State()
