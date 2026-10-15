from dishka import Provider, Scope, provide

from src.note.repositories import NoteRepository
from src.note.usecases.create_note import CreateNoteUseCase
from src.note.usecases.delete_note import DeleteNoteUseCase
from src.note.usecases.get_note import GetNoteUseCase
from src.note.usecases.list_notes import ListNotesUseCase
from src.note.usecases.summarize_notes import SummarizeNotesUseCase


class NoteProvider(Provider):
    scope = Scope.APP

    notes = provide(NoteRepository)
    create_note = provide(CreateNoteUseCase, scope=Scope.REQUEST)
    list_notes = provide(ListNotesUseCase, scope=Scope.REQUEST)
    get_note = provide(GetNoteUseCase, scope=Scope.REQUEST)
    delete_note = provide(DeleteNoteUseCase, scope=Scope.REQUEST)
    summarize_notes = provide(SummarizeNotesUseCase, scope=Scope.REQUEST)
