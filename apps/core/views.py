from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import FileResponse, JsonResponse, Http404
import redis

from apps.core.models import OrderPhoto
from apps.core.storage import LocalMediaStorage


def health(request):
    db_ok = False
    redis_ok = False

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            db_ok = cursor.fetchone() == (1,)
    except Exception:
        db_ok = False

    try:
        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        redis_ok = bool(client.ping())
    except Exception:
        redis_ok = False

    status = 200 if db_ok and redis_ok else 503
    return JsonResponse({"status": "ok" if status == 200 else "degraded", "database": db_ok, "redis": redis_ok}, status=status)


@staff_member_required
def order_photo_file(request, photo_id):
    try:
        photo = OrderPhoto.objects.get(pk=photo_id)
    except OrderPhoto.DoesNotExist as exc:
        raise Http404 from exc

    storage = LocalMediaStorage()
    if not storage.exists(photo.storage_key):
        raise Http404

    return FileResponse(
        storage.open(photo.storage_key),
        as_attachment=False,
        filename=photo.original_filename or f"photo-{photo.pk}",
        content_type=photo.mime_type or "application/octet-stream",
    )
