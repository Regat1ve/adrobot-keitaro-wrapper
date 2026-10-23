from django.urls import path
from django.views.generic import RedirectView

from campaigns import views

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='campaigns')),
    path('campaigns/', views.campaign_list, name='campaigns'),
    path('campaigns/new/', views.campaign_new, name='campaign_new'),
    path('campaigns/open/', views.campaign_open, name='campaign_open'),
    path('campaigns/<int:kt_id>/', views.campaign_detail, name='campaign'),
    path('campaigns/<int:kt_id>/fetch/', views.campaign_fetch, name='campaign_fetch'),
    path('streams/<int:stream_id>/<str:action>/', views.stream_action, name='stream_action'),
    path('links/<int:link_id>/<str:action>/', views.link_action, name='link_action'),
    path('api/offers/', views.offers_api, name='offers_api'),
]
