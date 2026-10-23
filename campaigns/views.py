import re

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import services
from .keitaro import Keitaro, KeitaroError
from .models import Campaign, OfferLink, Stream
from .services import EditorError


def _int(value) -> int | None:
    """'18813 — Flexacil' и '18813' дают 18813: так приходит значение из подсказки офферов."""
    match = re.match(r'\s*(\d+)', value or '')
    return int(match.group(1)) if match else None


def _safe(request, fn, *args, success: str = '', **kwargs):
    """Вызывает действие; ошибку показывает пользователю. Возвращает результат или True, при ошибке None."""
    try:
        result = fn(*args, **kwargs)
    except (EditorError, KeitaroError) as e:
        messages.error(request, str(e))
        return None
    if success:
        messages.success(request, success)
    return True if result is None else result


def campaign_list(request):
    q = request.GET.get('q', '').strip().lower()
    try:
        campaigns = Keitaro().campaigns()
    except KeitaroError as e:
        messages.error(request, str(e))
        campaigns = []
    if q:
        campaigns = [c for c in campaigns if q in c['name'].lower() or q == str(c['id'])]
    local = set(Campaign.objects.values_list('kt_id', flat=True))
    return render(request, 'campaigns/list.html', {'campaigns': campaigns[:200], 'q': q, 'local': local})


def offers_api(request):
    """Подсказка для поиска оффера: ищем по ID и по названию."""
    q = request.GET.get('q', '').strip().lower()
    try:
        offers = Keitaro().offers()
    except KeitaroError as e:
        return JsonResponse({'error': str(e)}, status=502)
    found = [o for o in offers if o.get('state') == 'active' and (not q or q in str(o['id']) or q in o['name'].lower())]
    return JsonResponse({'offers': [{'id': o['id'], 'name': o['name']} for o in found[:20]]})


def campaign_new(request):
    kt = Keitaro()
    lookups = {}
    for key, fn in (('domains', kt.domains), ('groups', kt.campaign_groups), ('sources', kt.traffic_sources)):
        try:
            lookups[key] = fn()
        except KeitaroError:
            lookups[key] = []  # у тестового пользователя на часть справочников нет прав
    form = request.POST if request.method == 'POST' else {}
    if request.method == 'POST':
        try:
            name = form.get('name', '').strip()
            if not name:
                raise EditorError('Укажите название кампании')
            offer_id = _int(form.get('offer'))
            if not offer_id:
                raise EditorError('Выберите оффер')
            campaign = services.create_campaign(
                kt, name=name, geo=services.parse_geo(form.get('geo', '')), offer_id=offer_id,
                domain_id=_int(form.get('domain_id')), group_id=_int(form.get('group_id')),
                traffic_source_id=_int(form.get('traffic_source_id')),
            )
        except (EditorError, KeitaroError) as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f'Кампания «{campaign.name}» создана в Keitaro, ID {campaign.kt_id}')
            return redirect('campaign', kt_id=campaign.kt_id)
    return render(request, 'campaigns/new.html', {'form': form, **lookups})


def campaign_detail(request, kt_id: int):
    campaign = Campaign.objects.filter(kt_id=kt_id).first()
    if campaign is None:
        campaign = _safe(request, services.open_campaign, kt_id, Keitaro())
        if campaign is None:
            return redirect('campaigns')
    streams = campaign.streams.prefetch_related('links')
    return render(request, 'campaigns/detail.html', {
        'campaign': campaign, 'streams': streams,
        'kt_url': f'{settings.KEITARO_URL.rstrip("/")}/admin/#!/campaigns/{kt_id}',
    })


@require_POST
def campaign_open(request):
    kt_id = _int(request.POST.get('kt_id'))
    return redirect('campaign', kt_id=kt_id) if kt_id else redirect('campaigns')


@require_POST
def campaign_fetch(request, kt_id: int):
    campaign = get_object_or_404(Campaign, kt_id=kt_id)
    _safe(request, services.fetch_streams, campaign, Keitaro(), success='Потоки загружены из Keitaro')
    return redirect('campaign', kt_id=kt_id)


def _stream_action(request, stream_id: int, action: str):
    stream = get_object_or_404(Stream, id=stream_id)
    if action == 'add':
        offer_id = _int(request.POST.get('offer'))
        if offer_id:
            _safe(request, services.add_offer, stream, offer_id, Keitaro())
        else:
            messages.error(request, 'Выберите оффер')
    elif action == 'push':
        _safe(request, services.push, stream, Keitaro(), success=f'Поток «{stream.name}» опубликован в Keitaro')
    elif action == 'cancel':
        services.cancel(stream)
    return redirect(f'/campaigns/{stream.campaign.kt_id}/#stream-{stream.id}')


def _link_action(request, link_id: int, action: str):
    link = get_object_or_404(OfferLink.objects.select_related('stream__campaign'), id=link_id)
    stream = link.stream
    if action == 'remove':
        _safe(request, services.remove_offer, link)
    elif action == 'restore':
        _safe(request, services.restore_offer, link)
    elif action == 'pin':
        _safe(request, services.pin_offer, link, _int(request.POST.get('share')))
    elif action == 'unpin':
        _safe(request, services.unpin_offer, link)
    return redirect(f'/campaigns/{stream.campaign.kt_id}/#stream-{stream.id}')


stream_action = require_POST(_stream_action)
link_action = require_POST(_link_action)
