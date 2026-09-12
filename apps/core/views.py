from django.db import connection
from django.http import JsonResponse
import redis
from django.conf import settings


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
