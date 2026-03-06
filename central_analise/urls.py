from django.urls import path
from . import views

urlpatterns = [
    path('status/', views.central_analise_status, name='central_analise_status'),
    path('chat/', views.chat_view, name='central_analise_chat'),
    path('dashboard/', views.dashboard_data, name='central_analise_dashboard'),
    path('operador/<str:operadora>/', views.operador_detail, name='central_analise_operador'),
]
