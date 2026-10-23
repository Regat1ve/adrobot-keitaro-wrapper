from django.db import models


class Campaign(models.Model):
    kt_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    fetched_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.name} [{self.kt_id}]'


class Stream(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='streams')
    kt_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    position = models.IntegerField(default=0)
    # поток без офферов показываем как есть и не разбираем: фильтры и редиректы обёртке не нужны
    has_offers = models.BooleanField(default=False)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.name

    @property
    def live_links(self):
        return [link for link in self.links.all() if link.is_live]

    @property
    def is_dirty(self):
        """Жёлтый поток: есть изменения, которых ещё нет в Keitaro."""
        return any(link.state in OfferLink.PENDING or link.share != link.published_share for link in self.links.all())


class OfferLink(models.Model):
    ACTIVE = 'active'        # опубликован, есть в Keitaro
    NEW = 'new'              # добавлен у нас, ещё не опубликован
    REMOVING = 'removing'    # удалён у нас, в Keitaro ещё есть
    REMOVED = 'removed'      # в архиве: в Keitaro его нет, можно вернуть
    RESTORING = 'restoring'  # возвращён из архива, ещё не опубликован
    STATES = [(ACTIVE, 'опубликован'), (NEW, 'новый'), (REMOVING, 'удаляется'), (REMOVED, 'архив'),
              (RESTORING, 'возвращается')]
    PENDING = {NEW, REMOVING, RESTORING}
    LIVE = {ACTIVE, NEW, RESTORING}  # получат трафик после публикации

    stream = models.ForeignKey(Stream, on_delete=models.CASCADE, related_name='links')
    offer_id = models.PositiveIntegerField()
    offer_name = models.CharField(max_length=500, blank=True)
    state = models.CharField(max_length=10, choices=STATES, default=NEW)
    share = models.PositiveSmallIntegerField(default=0)
    published_share = models.PositiveSmallIntegerField(default=0)
    pinned = models.BooleanField(default=False)

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['stream', 'offer_id'], name='uniq_offer_in_stream')]

    def __str__(self):
        return f'{self.offer_id} {self.offer_name}'

    @property
    def is_live(self):
        return self.state in self.LIVE
