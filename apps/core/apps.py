from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self):
        from .final_delivery_console import install_final_delivery_console
        from .preview_delivery_console import install_preview_delivery_console
        from .production_console import install_production_console
        from .qc_console import install_qc_console

        install_production_console()
        install_preview_delivery_console()
        install_qc_console()
        install_final_delivery_console()
