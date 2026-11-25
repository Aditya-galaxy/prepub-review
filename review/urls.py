from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("api/depositions/", views.DepositionList.as_view(), name="deposition-list"),
    path("api/deposit/", views.DepositUpload.as_view(), name="deposit"),
    path("api/depositions/<str:accession>/", views.DepositionDetail.as_view(), name="deposition-detail"),
    path("api/depositions/<str:accession>/coordinates/", views.DepositionCoordinates.as_view(),
         name="deposition-coordinates"),
    path("api/session/", views.SessionView.as_view(), name="session"),
    path("api/mine/", views.MyDepositions.as_view(), name="my-depositions"),
    path("api/depositions/<str:accession>/release/", views.ReleaseNow.as_view(),
         name="release"),
    path("auth/orcid/login/", views.orcid_login, name="orcid-login"),
    path("auth/orcid/callback/", views.orcid_callback, name="orcid-callback"),
    path("auth/orcid/logout/", views.orcid_logout, name="orcid-logout"),
    # ensure_csrf_cookie so the page can send the token with an upload. Without it the first
    # deposit of a session would fail CSRF for want of a cookie that was never set.
    path("", ensure_csrf_cookie(TemplateView.as_view(template_name="review/reviewer.html")),
         name="reviewer"),
]
