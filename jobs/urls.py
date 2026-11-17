from django.urls import path

from jobs import views

app_name = "jobs"

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.index, name="index"),
    path("jobs/", views.job_list, name="list"),
    path("jobs/<int:pk>/", views.detail, name="detail"),
    path("jobs/<int:pk>/status/", views.status_fragment, name="status"),
    path("jobs/<int:pk>/cancel/", views.cancel, name="cancel"),
    path("jobs/<int:pk>/result.zip", views.download, name="download"),
    path("jobs/<int:pk>/preview.pdb", views.preview, name="preview"),
    path("queue/", views.queue_status, name="queue"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
    path("handover/", views.handover, name="handover"),
    path("account/key/", views.credential, name="credential"),
    path("signup/", views.signup, name="signup"),
    path("models/<slug:slug>/", views.submit, name="submit"),
]
