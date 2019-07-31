import inflection
import jsonpatch
import kubernetes
import kubernetes.client.rest
import logging
import os
import os.path
import re
import threading
import time

def filter_patch_item(update_filters, item):
    if not update_filters:
        return True
    path = item['path']
    op = item['op']
    for f in update_filters:
        allowed_ops = f.get('allowedOps', ['add','remove','replace'])
        if op in allowed_ops and re.match(f['pathMatch'] + '$', path):
            return True
    return False

def create_patch(resource, update, update_filters):
    return [
        item for item in jsonpatch.JsonPatch.from_diff(
            resource,
            update
        ) if filter_patch_item(update_filters, item)
    ]

class CacheEntry(object):
    def __init__(self, resource, cache_resource):
        self.uid = resource['metadata']['uid']
        self.resource_version = resource['metadata']['resourceVersion']
        self.resource = resource if cache_resource else None

class WatchEvent(object):
    def __init__(self, event_type, resource):
        self.event_type = event_type
        self.resource = resource
        self.cached_resource = None

        if 'deletionTimestamp' in resource['metadata']:
            self.deleted = True
            self.delete_finalized = 0 == len(resource['metadata'].get('finalizers', []))
        else:
            self.deleted = False
            self.delete_finalized = False

        if self.event_type == 'ADDED':
            self.added = True
            self.modified = False
        elif self.event_type == 'MODIFIED':
            self.added = False
            self.modified = True
        elif self.event_type == 'DELETED':
            self.added = False
            self.modified = True

class Watcher(object):
    def __init__(self, operative, plural,
        cache_resources=False,
        group=None,
        name=None,
        namespace=None,
        preload=False,
        version='v1'
    ):
        self.cache_resources = cache_resources
        self.cache = {}
        self.name = name
        self.operative = operative
        self._preload = preload
        self.thread = threading.Thread(
            name = self.name,
            target = self.watch_loop
        )
        if group:
            self.__init_custom_resource_watcher(
                group=group,
                namespace=namespace,
                plural=plural,
                version=version
            )
        else:
            self.__init_core_resource_watcher(
                plural=plural,
                version=version
            )

    def __init_custom_resource_watcher(self, group, namespace, plural, version):
        if namespace:
            self.method = self.operative.custom_objects_api.list_namespaced_custom_object
            self.method_args = (
                group,
                version,
                namespace,
                plural
            )
        else:
            self.method = self.operative.custom_objects_api.list_cluster_custom_object
            self.method_args = (
                group,
                version,
                plural
            )

    def __call__(self, handler):
        self.handler=handler

    def get_cached_resource(self, resource_name):
        cache_hit = self.cache.get(resource_name, None)
        if cache_hit:
            return cache_hit.resource
        else:
            return None

    def make_event(self, event_type, resource):
        resource_name = resource['metadata']['name']
        event = WatchEvent(
            event_type=event_type,
            resource=resource
        )

        cache_hit = self.cache.get(resource_name, None)
        if cache_hit:
            if cache_hit.uid == resource['metadata']['uid']:
                if cache_hit.resource_version == resource['metadata']['resourceVersion']:
                    # Resource has not changed since it was last seen
                    return None
                event.added = False
                event.modified = True

                event.cached_resource = cache_hit.resource
            else:
                self.operative.logger.warn('Stale data found in cache for %s', resource_name)

        if event.delete_finalized:
            del self.cache[resource_name]
        else:
            self.cache[resource_name] = CacheEntry(resource, self.cache_resources)

        return event

    def preload(self):
        if not self._preload:
            return
        res = self.method(*self.method_args)
        for resource in res.get('items', []):
            event = self.make_event('ADDED', resource)
            if event:
                try:
                    self.handler(event)
                except Exception as e:
                    self.operative.logger.exception("Error handling event %s", event)

    def watch_loop(self):
        while True:
            try:
                self.watch()
            except Exception as e:
                self.operative.logger.exception("Error in watch_loop: %s", e)
                time.sleep(60)

    def watch(self):
        stream = kubernetes.watch.Watch().stream(self.method, *self.method_args)
        for event in stream:
            self.operative.logger.debug(event)
            event_obj = event['object']
            # FIXME - Check better, kind 'Status' could conflict with a crd?
            if event_obj['kind'] == 'Status':
                self.operative.logger.debug('Watch %s - reason %s, %s',
                    event_obj['status'],
                    event_obj['reason'],
                    event_obj['message']
                )
                if event_obj['status'] == 'Failure':
                    if event_obj['reason'] == 'Expired':
                        return
                    else:
                        raise Exception("Watch failure: " + event_obj['message'])
            else:
                event = self.make_event(event['type'], event_obj)
                if event:
                    try:
                        self.handler(event)
                    except Exception as e:
                        self.operative.logger.exception("Error handling event %s", event)

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

class KubeOperative(object):

    def __init__(
        self,
        logging_format='%(asctime)s %(threadName)s %(levelname)s - %(message)s',
        logging_level=logging.INFO,
        operator_domain=None,
        operator_namespace=None
    ):
        self.api_groups = {}
        self.watcher_list = []
        self.watchers = {}
        self.__init_logger(logging_format, logging_level)
        self.__init_domain(operator_domain)
        self.__init_namespace(operator_namespace)
        self.__init_kube_apis()

    def __init_domain(self, operator_domain):
        if operator_domain:
            self.operator_domain = operator_domain
        else:
            self.operator_domain = os.environ.get('OPERATOR_DOMAIN', 'gpte.redhat.com')

    def __init_logger(self, logging_format, logging_level):
        handler = logging.StreamHandler()
        handler.setLevel(logging_level)
        handler.setFormatter(
            logging.Formatter(logging_format)
        )
        self.logger = logging.getLogger('operator')
        self.logger.addHandler(handler)

    def __init_namespace(self, operator_namespace):
        if operator_namespace:
            self.operator_namespace = operator_namespace
        elif 'OPERATOR_NAMESPACE' in os.environ:
            self.operator_namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.operator_namespace = f.read()
        else:
            self.operator_namespace = 'resource-claim-operator'

    def __init_kube_apis(self):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/token'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/token')
            kube_auth_token = f.read()
            kube_config = kubernetes.client.Configuration()
            kube_config.api_key['authorization'] = kube_auth_token
            kube_config.api_key_prefix['authorization'] = 'Bearer'
            kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
            kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        else:
            kubernetes.config.load_kube_config()
            kube_config = None

        self.core_v1_api = kubernetes.client.CoreV1Api(
            kubernetes.client.ApiClient(kube_config)
        )
        self.custom_objects_api = kubernetes.client.CustomObjectsApi(
            kubernetes.client.ApiClient(kube_config)
        )

    def create_resource(self, resource_definition):
        if '/' in resource_definition['apiVersion']:
            return self.create_custom_resource(resource_definition)
        else:
            return self.create_core_resource(resource_definition)

    def create_core_resource(self, resource_definition):
        namespace = resource_definition['metadata'].get('namespace', None)
        if namespace:
            method = getattr(
                self.core_v1_api,
                'create_namespaced_' + inflection.underscore(kind)
            )
            return method(namespace, resource_definition)
        else:
            method = getattr(
                self.core_v1_api,
                'create_' + inflection.underscore(kind)
            )
            return method(resource_definition)

    def create_custom_resource(self, resource_definition):
        group, version = resource_definition['apiVersion'].split('/')
        namespace = resource_definition['metadata'].get('namespace', None)
        plural = self.kind_to_plural(group, version, resource_definition['kind'])
        if namespace:
            return self.custom_objects_api.create_namespaced_custom_object(
                group,
                version,
                namespace,
                plural,
                resource_definition
            )
        else:
            return self.custom_objects_api.create_cluster_custom_object(
                group,
                version,
                plural,
                resource_definition
            )

    def get_resource(self, api_version, kind, name, namespace=None):
        if '/' in api_version:
            group, version = api_version.split('/')
            return self.get_custom_resource(
                group=group,
                version=version,
                kind=kind,
                name=name,
                namespace=namespace
            )
        else:
            return self.get_core_resource(
                kind=kind,
                name=name,
                namespace=namespace
            )

    def get_core_resource(self, kind, namespace, name):
        try:
            if namespace:
                method = getattr(
                    self.core_v1_api,
                    'read_namespaced_' + inflection.underscore(kind)
                )
                return method(name, namespace)
            else:
                method = getattr(
                    self.core_v1_api,
                    'read_' + inflection.underscore(kind)
                )
                return method(name)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def get_custom_resource(self, group, version, kind, namespace, name):
        plural = self.kind_to_plural(group, version, kind)
        try:
            if namespace:
                return self.custom_objects_api.get_namespaced_custom_object(
                    group,
                    version,
                    namespace,
                    plural,
                    name
                )
            else:
                return self.custom_objects_api.get_cluster_custom_object(
                    group,
                    version,
                    plural,
                    name
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def kind_to_plural(self, group, version, kind):
        if group in self.api_groups \
        and version in self.api_groups[group]:
            for resource in self.api_groups[group][version]['resources']:
                if resource['kind'] == kind:
                    return resource['name']

        resp = self.custom_objects_api.api_client.call_api(
            '/apis/{}/{}'.format(group,version),
            'GET',
            response_type='object',
        )
        group_info = resp[0]
        if group not in self.api_groups:
            self.api_groups[group] = {}
        self.api_groups[group][version] = group_info

        for resource in group_info['resources']:
            if resource['kind'] == kind:
                return resource['name']
        raise Exception('Unable to find kind {} in {}/{}', kind, group, version)

    def patch_core_resource(self, kind, namespace, name, patch):
        if namespace:
            method = getattr(
                self.core_v1_api,
                'patch_namespaced_' + inflection.underscore(kind)
            )
            return method(name, namespace, patch)
        else:
            method = getattr(
                self.core_v1_api,
                'patch_' + inflection.underscore(kind)
            )
            return method(name, patch)

    def patch_custom_resource(self, group, version, kind, namespace, name, patch):
        plural = self.kind_to_plural(group, version, kind)
        if namespace:
            return self.custom_objects_api.patch_namespaced_custom_object(
                group,
                version,
                namespace,
                plural,
                name,
                patch
            )
        else:
            return self.custom_objects_api.patch_cluster_custom_object(
                group,
                version,
                plural,
                name,
                patch
            )

    def patch_core_resource_status(
        self,
        name,
        patch,
        kind=None,
        plural=None
    ):
        # FIXME
        pass

    def patch_custom_resource_status(
        self,
        name,
        patch,
        kind=None,
        plural=None,
        group=None,
        namespace=None,
        version=None
    ):
        if plural == None:
            if kind == None:
                raise Exception("Either plural or kind must be provided")
            plural = self.kind_to_plural(group, version, kind)
        if group == None:
            group = self.operator_domain
        if namespace == None:
            namespace = self.operator_namespace
        if version == None:
            version = self.operator_version

        if namespace:
            return self.custom_objects_api.patch_namespaced_custom_object_status(
                group, version, namespace, plural, name, patch
            )
        else:
            return self.custom_objects_api.patch_cluster_custom_object_status(
                group, version, plural, name, patch
            )

    def patch_resource(self, resource, patch, update_filters=None):
        if not isinstance(patch, list):
            patch = create_patch(resource, patch, update_filters)
        if not patch:
            return resource

        if '/' in resource['apiVersion']:
            group, version = resource['apiVersion'].split('/')
            return self.patch_custom_resource(
                group,
                version,
                resource['kind'],
                resource['metadata'].get('namespace', None),
                resource['metadata']['name'],
                patch
            )
        else:
            return self.patch_core_resource(
                resource['kind'],
                resource['metadata'].get('namespace', None),
                resource['metadata']['name'],
                patch
            )

    def patch_resource_status(self, resource, patch, update_filters=None):
        if not isinstance(patch, list):
            patch = create_patch(resource, {"status": patch}, update_filters)
        if not patch:
            return resource

        if '/' in resource['apiVersion']:
            group, version = resource['apiVersion'].split('/')
            return self.patch_custom_resource_status(
                group=group,
                kind=resource['kind'],
                name=resource['metadata']['name'],
                namespace=resource['metadata'].get('namespace', None),
                patch=patch,
                version=version
            )
        else:
            return self.patch_core_resource_status(
                kind=resource['kind'],
                name=resource['metadata']['name'],
                namespace=resource['metadata'].get('namespace', None),
                patch=patch
            )

    def start_watchers(self):
        for w in self.watcher_list:
            w.preload()

        for w in self.watcher_list:
            w.start()

    def watcher(self, plural, cache_resources=False, name=None, namespace=None, group=None, preload=False, version='v1'):
        if not name:
            if group:
                if namespace:
                    name = '{}/{}:{}:{}'.format(group, version, namespace, plural)
                else:
                    name = '{}/{}:{}'.format(group, version, name)
            else:
                if namespace:
                    name = '{}:{}:{}'.format(version, namespace, plural)
                else:
                    name = '{}:{}'.format(version, plural)

        w = Watcher(
            cache_resources=cache_resources,
            group=group,
            name=name,
            namespace=namespace,
            operative=self,
            plural=plural,
            preload=preload,
            version=version
        )

        self.watcher_list.append(w)
        self.watchers[name] = w
        return w
