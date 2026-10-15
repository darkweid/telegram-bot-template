from dishka import Provider, Scope, provide

from src.admin.usecases.get_llm_usage_stats import GetLLMUsageStatsUseCase


class AdminProvider(Provider):
    get_llm_usage_stats = provide(GetLLMUsageStatsUseCase, scope=Scope.REQUEST)
