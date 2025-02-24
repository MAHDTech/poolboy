import inflection
import kubernetes
import logging
import os
import os.path
import re
import threading
import time

def _jsonpatch_path(*path):
    return '/' + '/'.join([
        p.replace('~', '~0').replace('/', '~1') for p in path
    ])

def _jsonpatch_from_diff(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        for op in _jsonpatch_from_dict_diff(a, b, path):
            yield op
    elif isinstance(a, list) and isinstance(b, list):
        for op in _jsonpatch_from_list_diff(a, b, path):
            yield op
    elif a != b:
        yield dict(op='replace', path=_jsonpatch_path(*path), value=b)

def _jsonpatch_from_dict_diff(a, b, path):
    for k, v in a.items():
        if k in b:
            for op in _jsonpatch_from_diff(v, b[k], path + [k]):
                yield op
        else:
            yield dict(op='remove', path=_jsonpatch_path(*path, k))
    for k, v in b.items():
        if k not in a:
            yield dict(op='add', path=_jsonpatch_path(*path, k), value=v)

def _jsonpatch_from_list_diff(a, b, path):
    for i in range(min(len(a), len(b))):
        for op in _jsonpatch_from_diff(a[i], b[i], path + [str(i)]):
            yield op
    for i in range(len(a) - 1, len(b) -1, -1):
        yield dict(op='remove', path=_jsonpatch_path(*path, str(i)))
    for i in range(len(a), len(b)):
        yield dict(op='add', path=_jsonpatch_path(*path, str(i)), value=b[i])

def jsonpatch_from_diff(a, b):
    return [ item for item in _jsonpatch_from_diff(a, b, []) ]

def filter_patch_item(update_filters, item):
    if not update_filters:
        return True
    path = item['path']
    op = item['op']
    for f in update_filters:
        allowed_ops = f.get('allowedOps', ['add','remove','replace'])
        if re.match(f['pathMatch'] + '$', path):
            if op not in allowed_ops:
                return False
            return True
    return False

def create_patch(resource, update, update_filters=None):
    # FIXME - There should be some sort of warning about patch items being rejected?
    return [
        item for item in jsonpatch_from_diff(
            resource,
            update
        ) if filter_patch_item(update_filters, item)
    ]

class Watcher(object):
    def __init__(self, operative, kind,
        group=None,
        name=None,
        namespace=None,
        preload=False,
        version='v1'
    ):
        self.logger = logging.getLogger('watch.{}/{}'.format(namespace, name) if namespace else 'watch.{}'.format(name))
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
                kind=kind,
                version=version
            )
        else:
            self.__init_core_resource_watcher(
                namespace=namespace,
                kind=kind,
                version=version
            )

    def __init_core_resource_watcher(self, namespace, kind, version):
        if namespace:
            self.method = getattr(
                self.operative.core_v1_api,
                'list_namespaced_' + inflection.underscore(kind)
            )
            self.method_args = (namespace,)
        else:
            self.method = getattr(
                self.operative.core_v1_api,
                'list_' + inflection.underscore(kind)
            )
            self.method_args = ()

    def __init_custom_resource_watcher(self, group, namespace, kind, version):
        plural = self.operative.kind_to_plural(group, version, kind)
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

    def watch_loop(self):
        while True:
            try:
                self.watch()
            except Exception as e:
                if isinstance(e, kubernetes.client.rest.ApiException) \
                and e.status == 403:
                    self.logger.exception("Forbidden response on watch: %s", e)
                    del self.operative.watchers[self.name]
                    return
                else:
                    self.logger.exception("Error in watch loop: %s", e)
                    time.sleep(30)

    def watch(self):
        stream = kubernetes.watch.Watch().stream(self.method, *self.method_args)
        for event in stream:
            # Exit without processing if watch was removed
            if not self.name in self.operative.watchers:
                return

            # Handle event object
            event_obj = event['object']
            if event['type'] == 'ERROR' \
            and event_obj['kind'] == 'Status':
                self.logger.debug('Watch %s - reason %s, %s',
                    event_obj['status'],
                    event_obj['reason'],
                    event_obj['message']
                )
                if event_obj['status'] == 'Failure':
                    if event_obj['reason'] in ('Expired', 'Gone'):
                        self.logger.info('Restarting watch %s, reason %s', self.method_args, event_obj['reason'])
                        return
                    else:
                        raise Exception("Watch failure: reason {}, message {}".format(event_obj['reason'], event_obj['message']))
            else:
                try:
                    self.handler(event, self.logger)
                except Exception as e:
                    self.logger.exception("Error handling event %s", event)

    def start(self):
        if not self.thread.is_alive():
            self.thread.start()

class KubeOperative(object):

    def __init__(
        self,
        operator_domain=None,
        operator_namespace=None
    ):
        self.api_groups = {}
        self.watchers = {}
        self.__init_logger()
        self.__init_domain(operator_domain)
        self.__init_namespace(operator_namespace)
        self.__init_kube_apis()

    def __init_domain(self, operator_domain):
        if operator_domain:
            self.operator_domain = operator_domain
        else:
            self.operator_domain = os.environ.get('OPERATOR_DOMAIN', 'poolboy.gpte.redhat.com')
        self.version = 'v1'
        self.api_version = self.operator_domain + '/' + self.version

    def __init_logger(self):
        self.logger = logging.getLogger('operator')

    def __init_namespace(self, operator_namespace):
        if operator_namespace:
            self.operator_namespace = operator_namespace
        elif 'OPERATOR_NAMESPACE' in os.environ:
            self.operator_namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.operator_namespace = f.read()
        else:
            self.operator_namespace = 'poolboy'

    def __init_kube_apis(self):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes.config.load_incluster_config()
        else:
            kubernetes.config.load_kube_config()

        self.core_v1_api = kubernetes.client.CoreV1Api()
        self.custom_objects_api = kubernetes.client.CustomObjectsApi()

        # Hack to allow json-patch, hopefully we can remove this in the future
        self.custom_objects_api_jsonpatch = kubernetes.client.CustomObjectsApi()
        self.custom_objects_api_jsonpatch.api_client.select_header_content_type = \
            lambda _ : 'application/json-patch+json'

    def create_resource(self, resource_definition):
        if '/' in resource_definition['apiVersion']:
            return self.create_custom_resource(resource_definition)
        else:
            return self.create_core_resource(resource_definition)

    def create_core_resource(self, resource_definition):
        kind = resource_definition['kind']
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

    def delete_resource(self, api_version, kind, name, namespace=None):
        if '/' in api_version:
            group, version = api_version.split('/')
            return self.delete_custom_resource(
                group=group,
                version=version,
                kind=kind,
                name=name,
                namespace=namespace
            )
        else:
            return self.delete_core_resource(
                kind=kind,
                name=name,
                namespace=namespace
            )

    def delete_core_resource(self, kind, namespace, name):
        try:
            if namespace:
                method = getattr(
                    self.core_v1_api,
                    'delete_namespaced_' + inflection.underscore(kind)
                )
                return method(name, namespace)
            else:
                method = getattr(
                    self.core_v1_api,
                    'delete_' + inflection.underscore(kind)
                )
                return method(name)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def delete_custom_resource(self, group, version, kind, namespace, name):
        plural = self.kind_to_plural(group, version, kind)
        try:
            if namespace:
                return self.custom_objects_api.delete_namespaced_custom_object(
                    group,
                    version,
                    namespace,
                    plural,
                    name
                )
            else:
                return self.custom_objects_api.delete_cluster_custom_object(
                    group,
                    version,
                    plural,
                    name
                )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

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
            resource = self.get_core_resource(
                kind=kind,
                name=name,
                namespace=namespace
            )
            if resource:
                return self.core_v1_api.api_client.sanitize_for_serialization(resource)
            else:
                return None

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
            auth_settings=['BearerToken'],
            response_type='object'
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

        # Hack to allow json-patch, hopefully we can remove this in the future
        save_select_header_content_type = self.custom_objects_api.api_client.select_header_content_type

        try:
            if namespace:
                method = getattr(
                    self.core_v1_api,
                    'patch_namespaced_' + inflection.underscore(kind)
                )
                ret = method(name, namespace, patch)
            else:
                method = getattr(
                    self.core_v1_api,
                    'patch_' + inflection.underscore(kind)
                )
                ret = method(name, patch)
        finally:
            self.custom_objects_api.api_client.select_header_content_type = save_select_header_content_type
        return ret

    def patch_custom_resource(self, group, version, kind, namespace, name, patch):
        plural = self.kind_to_plural(group, version, kind)

        if namespace:
            ret = self.custom_objects_api_jsonpatch.patch_namespaced_custom_object(
                group,
                version,
                namespace,
                plural,
                name,
                patch
            )
        else:
            ret = self.custom_objects_api_jsonpatch.patch_cluster_custom_object(
                group,
                version,
                plural,
                name,
                patch
            )

        return ret

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
            return resource, False

        if '/' in resource['apiVersion']:
            group, version = resource['apiVersion'].split('/')
            return self.patch_custom_resource(
                group,
                version,
                resource['kind'],
                resource['metadata'].get('namespace', None),
                resource['metadata']['name'],
                patch
            ), True
        else:
            return self.patch_core_resource(
                resource['kind'],
                resource['metadata'].get('namespace', None),
                resource['metadata']['name'],
                patch
            ), True

    def patch_resource_status(self, resource, patch, update_filters=None):
        if not isinstance(patch, list):
            patch = create_patch(resource, {"status": patch}, update_filters)
        if not patch:
            return resource, False

        if '/' in resource['apiVersion']:
            group, version = resource['apiVersion'].split('/')
            return self.patch_custom_resource_status(
                group=group,
                kind=resource['kind'],
                name=resource['metadata']['name'],
                namespace=resource['metadata'].get('namespace', None),
                patch=patch,
                version=version
            ), True
        else:
            return self.patch_core_resource_status(
                kind=resource['kind'],
                name=resource['metadata']['name'],
                namespace=resource['metadata'].get('namespace', None),
                patch=patch
            ), True

    def create_watcher(self, kind, name=None, namespace=None, group=None, preload=False, version='v1'):
        if not name:
            if group:
                if namespace:
                    name = '{}/{}:{}:{}'.format(group, version, namespace, kind)
                else:
                    name = '{}/{}:{}'.format(group, version, name)
            else:
                if namespace:
                    name = '{}:{}:{}'.format(version, namespace, kind)
                else:
                    name = '{}:{}'.format(version, kind)

        w = Watcher(
            group=group,
            name=name,
            namespace=namespace,
            operative=self,
            kind=kind,
            preload=preload,
            version=version
        )

        self.watchers[name] = w
        return w
