import hmac

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

def app_download(request, token):
    """The latest Android build. 404 — never 403 — for a wrong or unset
    token, so the URL's existence isn't confirmed to anyone guessing."""
    expected = settings.APP_DOWNLOAD_TOKEN
    if not expected or not hmac.compare_digest(token, expected):
        raise Http404
    apk = settings.APP_DOWNLOAD_DIR / "krono.apk"
    if not apk.is_file():
        raise Http404
    return FileResponse(
        open(apk, "rb"),
        as_attachment=True,
        filename="Krono.apk",
        content_type="application/vnd.android.package-archive",
    )


urlpatterns = [
    path("app/<str:token>/krono.apk", app_download, name="app-download"),
    path("admin/", admin.site.urls),
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/", include("capture.urls")),
    path("api/", include("graph.urls")),
    path("api/", include("pipeline.urls")),
]
