import functools
import json
import threading
import time

from redis import StrictRedis

from domainmodel.domain_model import DomainModel


class EventStore(object):
    """
    Event Store class.
    """

    def __init__(self, host='localhost', port=6379):
        """
        :param host: The Redis host.
        :param port: The Redis port.
        """
        self.redis = StrictRedis(decode_responses=True, host=host, port=port)
        self.subscribers = {}
        self.entity_cache_handlers = {}
        self.domain_model = DomainModel(self.redis)

    def publish(self, _id, _topic, _action, _entity):
        """
        Publish an event.

        :param _id:
        :param _topic:
        :param _action:
        :param _entity:
        :return: The entry ID.
        """
        key = 'events:{{{0}}}_{1}'.format(_topic, _action)
        entry_id = self.redis.xadd(
            key,
            {'event_id': _id, 'event_entity': _entity},
            id='{0:.6f}'.format(time.time()).replace('.', '-')
        )

        return entry_id

    def subscribe(self, _topic, _action, _handler):
        """
        Subscribe to an event channel.

        :param _topic: The event topic.
        :param _action: The event action.
        :param _handler: The event handler.
        :return: Success.
        """
        if (_topic, _action) in self.subscribers:
            self.subscribers[(_topic, _action)].add_handler(_handler)
        else:
            subscriber = Subscriber(_topic, _action, _handler, self.redis)
            subscriber.start()
            self.subscribers[(_topic, _action)] = subscriber

        return True

    def unsubscribe(self, _topic, _action, _handler):
        """
        Unsubscribe from an event channel.

        :param _topic: The event topic.
        :param _action: The event action.
        :param _handler: The event handler.
        :return: Success.
        """
        subscriber = self.subscribers.get((_topic, _action))
        if not subscriber:
            return False

        subscriber.rem_handler(_handler)
        if not subscriber:
            subscriber.stop()
            del self.subscribers[(_topic, _action)]

        return True

    def read(self, _last_id, _topic, _action):
        """
        Read new event stream entries.

        :param _last_id: The ID of the last entry read.
        :param _topic: The event topic.
        :param _action: The event action.
        :return: A list of event entries or None.
        """
        key = 'events:{{{0}}}_{1}'.format(_topic, _action)
        return self.redis.xread({key: _last_id}, block=1000)

    def find_one(self, _topic, _id):
        """
        Find an entity for a topic with an specific id.

        :param _topic: The event topic, i.e. name of entity.
        :param _id: The entity id.
        :return: A dict with the entity.
        """
        return self.find_all(_topic).get(_id)

    def find_all(self, _topic):
        """
        Find all entites for a topic.

        :param _topic: The event topic, i.e name of entity.
        :return: A dict mapping id -> entity.
        """

        def _get_entities(_events):
            entities = map(lambda x: json.loads(x[1]['event_entity']), _events)
            return dict(map(lambda x: (x['id'], x), entities))

        def _remove_deleted(_created, _deleted):
            for d in _deleted.keys():
                del _created[d]
            return _created

        def _set_updated(_created, _updated):
            for k, v in _updated.items():
                _created[k] = v
            return _created

        # read from cache
        if self.domain_model.exists(_topic):
            return self.domain_model.retrieve(_topic)

        # result is a dict mapping id -> entity
        result = {}

        # read all events at once
        with self.redis.pipeline() as pipe:
            pipe.multi()
            pipe.xrange('events:{{{0}}}_created'.format(_topic))
            pipe.xrange('events:{{{0}}}_deleted'.format(_topic))
            pipe.xrange('events:{{{0}}}_updated'.format(_topic))
            created_events, deleted_events, updated_events = pipe.execute()

        # get created entities
        if created_events:
            result = _get_entities(created_events)

        # remove deleted entities
        if deleted_events:
            result = _remove_deleted(result, _get_entities(deleted_events))

        # set updated entities
        if updated_events:
            result = _set_updated(result, _get_entities(updated_events))

        # write into cache
        for value in result.values():
            self.domain_model.create(_topic, value)

        return result

    def activate_entity_cache(self, _topic):
        """
        Keep entity cache up to date.

        :param _topic: The entity type.
        """
        created_handler = functools.partial(self._entity_created, _topic)
        self.entity_cache_handlers[(_topic, 'created')] = created_handler
        self.subscribe(_topic, 'created', created_handler)

        deleted_handler = functools.partial(self._entity_deleted, _topic)
        self.entity_cache_handlers[(_topic, 'deleted')] = deleted_handler
        self.subscribe(_topic, 'deleted', deleted_handler)

        updated_handler = functools.partial(self._entity_updated, _topic)
        self.entity_cache_handlers[(_topic, 'updated')] = updated_handler
        self.subscribe(_topic, 'updated', updated_handler)

    def deactivate_entity_cache(self, _topic):
        """
        Stop keeping entity cache up to date.

        :param _topic: The entity type.
        """
        created_handler = self.entity_cache_handlers[(_topic, 'created')]
        self.unsubscribe(_topic, 'created', created_handler)

        deleted_handler = self.entity_cache_handlers[(_topic, 'deleted')]
        self.unsubscribe(_topic, 'deleted', deleted_handler)

        updated_handler = self.entity_cache_handlers[(_topic, 'updated')]
        self.unsubscribe(_topic, 'updated', updated_handler)

    def _entity_created(self, _topic, _item):
        """
        Event handler for entity created events, i.e. create a cached entity.

        :param _topic: The entity type.
        :param _item: A dict with entity properties.
        """
        if self.domain_model.exists(_topic):
            entity = json.loads(_item[1][0][1]['event_entity'])
            self.domain_model.create(_topic, entity)

    def _entity_deleted(self, _topic, _item):
        """
        Event handler for entity deleted events, i.e. delete a cached entity.

        :param _topic: The entity type.
        :param _item: A dict with entity properties.
        """
        if self.domain_model.exists(_topic):
            entity = json.loads(_item[1][0][1]['event_entity'])
            self.domain_model.delete(_topic, entity)

    def _entity_updated(self, _topic, _item):
        """
        Event handler for entity updated events, i.e. update a cached entity.

        :param _topic: The entity type.
        :param _item: A dict with entity properties.
        """
        if self.domain_model.exists(_topic):
            entity = json.loads(_item[1][0][1]['event_entity'])
            self.domain_model.update(_topic, entity)


class Subscriber(threading.Thread):
    """
    Subscriber Thread class.
    """

    def __init__(self, _topic, _action, _handler, _redis):
        """
        :param _topic: The topic to subscirbe to.
        :param _action: The action to scubscribe to.
        :param _handler: A handler function.
        :param _redis: A Redis instance.
        """
        super(Subscriber, self).__init__()
        self._running = False
        self.key = 'events:{{{0}}}_{1}'.format(_topic, _action)
        self.subscribed = True
        self.handlers = [_handler]
        self.redis = _redis

    def __len__(self):
        return len(self.handlers)

    def run(self):
        """
        Poll the event stream and call each handler with each entry returned.
        """
        if self._running:
            return

        last_id = '$'
        self._running = True
        while self.subscribed:
            items = self.redis.xread({self.key: last_id}, block=1000) or []
            for item in items:
                for handler in self.handlers:
                    handler(item)
                last_id = item[1][0][0]
        self._running = False

    def stop(self):
        """
        Stop polling the event stream.
        """
        self.subscribed = False

    def add_handler(self, _handler):
        """
        Add an event handler.

        :param _handler: The event handler function.
        """
        self.handlers.append(_handler)

    def rem_handler(self, _handler):
        """
        Remove an event handler.

        :param _handler: The event handler function.
        """
        self.handlers.remove(_handler)
