from django.urls import path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("api/depositions/", views.DepositionList.as_view(), name="deposition-list"),
    path("api/depositions/<str:accession>/", views.DepositionDetail.as_view(), name="deposition-detail"),
    path("api/depositions/<str:accession>/coordinates/", views.DepositionCoordinates.as_view(),
         name="deposition-coordinates"),
    path("api/session/", views.SessionView.as_view(), name="session"),
    path("", TemplateView.as_view(template_name="review/reviewer.html"), name="reviewer"),
]
