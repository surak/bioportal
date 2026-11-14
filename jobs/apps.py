from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"

    def ready(self):
        from jobs import audit, registry  # noqa: F401  (registers signals)

        registry.load_specs()
