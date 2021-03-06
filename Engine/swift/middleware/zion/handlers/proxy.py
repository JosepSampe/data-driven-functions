from zion.handlers import BaseHandler
from zion.handlers.base import NotFunctionRequest
from swift.common.swob import HTTPNotFound, HTTPUnauthorized, Response
from swift.common.utils import public
from swift.common.wsgi import make_subrequest
from swiftclient.client import http_connection, quote
import os
import pickle
import random


class ProxyHandler(BaseHandler):

    def __init__(self, request, conf, app, logger, redis):
        super(ProxyHandler, self).__init__(
            request, conf, app, logger, redis)

        self.functions_container = self.conf["functions_container"]
        self.disaggregated_compute = self.conf["disaggregated_compute"]
        self.compute_nodes = self.conf["compute_nodes"]
        self.req.headers['functions-enabled'] = True

    def _parse_vaco(self):
        return self.req.split_path(3, 4, rest_with_last=True)

    def _get_functions(self):
        functions_data = dict()

        self.functions_list = {}
        self.parent_functions_list = {}

        functions_list = {}
        if self.obj:
            key = self.req.path
            functions_list = self.redis.hgetall(key)
        key = os.path.join('/', self.api_version, self.account, self.container)
        parent_functions_list = self.redis.hgetall(key)

        if functions_list:
            for key in functions_list:
                self.functions_list[key.decode()] = pickle.loads(functions_list[key])

        if parent_functions_list:
            for key in parent_functions_list:
                self.parent_functions_list[key.decode()] = pickle.loads(parent_functions_list[key])

        if self.method in self.function_methods:
            if self.method == 'GET':
                keys = self.get_keys
            elif self.method == 'PUT':
                keys = self.put_keys
            elif self.method == 'DELETE':
                keys = self.del_keys

            if self.parent_functions_list:
                for key in self.parent_functions_list:
                    if key in keys:
                        functions_data[key] = self.parent_functions_list[key]

            if self.functions_list:
                for key in self.functions_list:
                    if key in keys:
                        functions_data[key] = self.functions_list[key]

        return functions_data

    def handle_request(self):
        if hasattr(self, self.method) and self.is_valid_request:
            try:
                handler = getattr(self, self.method)
                getattr(handler, 'publicly_accessible')
            except AttributeError:
                raise NotFunctionRequest()
            return handler()
        else:
            raise NotFunctionRequest()

    def _verify_access(self, cont, obj):
        """
        Verifies access to the specified object in swift
        :param cont: swift container name
        :param obj: swift object name
        :raise HTTPNotFound: if the object doesn't exists in swift
        :return response: Object response
        """
        if obj:
            path = os.path.join('/', self.api_version, self.account, cont, obj)
        else:
            path = os.path.join('/', self.api_version, self.account, cont)
        self.logger.debug('Verifying access to %s' % path)

        new_env = dict(self.req.environ)
        if 'HTTP_TRANSFER_ENCODING' in new_env.keys():
            del new_env['HTTP_TRANSFER_ENCODING']

        auth_token = self.req.headers.get('X-Auth-Token')
        sub_req = make_subrequest(new_env, 'HEAD', path,
                                  headers={'X-Auth-Token': auth_token},
                                  swift_source='function_middleware')

        resp = sub_req.get_response(self.app)

        if not resp.is_success:
            if resp.status_int == 401:
                raise HTTPUnauthorized('Unauthorized to access to this '
                                       'resource: ' + path + '\n')
            else:
                raise HTTPNotFound('There was an error: "' + path +
                                   ' doesn\'t exists in Swift.\n')

    def _get_function_set_data(self):
        params = dict()
        header = [i for i in self.available_set_headers
                  if i in self.req.headers.keys()]
        if len(header) > 1:
            raise HTTPUnauthorized('The system can only set 1 '
                                   'function at a time.\n')

        trigger = header[0].lower().split('-', 2)[2]
        function = self.req.headers[header[0]]

        if self.req.body:
            params = self.req.body

        return trigger, function, params

    def _set_function(self):
        """
        Sets the specified function to the trigger of an object or a container
        """
        trigger, function, params = self._get_function_set_data()
        # Verify access to the function
        self._verify_access(self.functions_container, function)
        function_data = dict()
        function_data[function] = params
        key = self.req.path

        self._verify_access(self.container, self.obj)
        self.redis.hset(key, trigger, pickle.dumps(function_data))

        msg = 'Function "' + function + '" correctly ' \
              'assigned to the "' + trigger + '" trigger.\n'
        self.logger.info(msg)
        return Response(body=msg, headers={'etag': ''}, request=self.req)

    def _get_function_unset_data(self):
        header = [i for i in self.available_unset_headers
                  if i in self.req.headers.keys()]
        if len(header) > 1:
            raise HTTPUnauthorized('The system can only unset 1 '
                                   'function at a time.\n')

        trigger = header[0].lower().split('-', 2)[2].rsplit('-', 1)[0]
        function = self.req.headers[header[0]]

        return trigger, function

    def _unset_function(self):
        """
        Unsets the specified function from the trigger of an object or a container
        """
        trigger, function = self._get_function_unset_data()
        key = self.req.path
        redis_function_data = self.redis.hgetall(key)
        function_data = {}

        if redis_function_data:
            for trg in redis_function_data:
                fn_info = pickle.loads(redis_function_data[trg])
                function_data[trg.decode()] = fn_info

        if trigger in function_data and function in function_data[trigger]:
            self.redis.hdel(key, trigger)
            del function_data[trigger]
            if not function_data:
                self.redis.delete(key)
            msg = 'Function "' + function + '" correctly '\
                  ' removed from the "' + trigger + '" trigger.\n'
        else:
            msg = 'Error: Function "' + function + '" not'\
                  ' assigned to the "' + trigger + '" trigger.\n'
        self.logger.info(msg)

        return Response(body=msg, headers={'etag': ''},
                        request=self.req)

    def _check_mandatory_metadata(self):
        for key in self.mandatory_function_metadata:
            if 'X-Object-Meta-Function-'+key not in self.req.headers:
                return False
        return True

    def _set_headers(self):
        if 'Content-Type' in self.req.headers:
            self.req.headers.pop('Content-Type')
        if 'X-Domain-Name' in self.req.headers:
            self.req.headers.pop('X-Domain-Name')
        if 'X-Domain-Id' in self.req.headers:
            self.req.headers.pop('X-Domain-Id')

    def _prepare_connection(self):
        self._set_headers()
        compute_nodes = self.compute_nodes.split(',')
        compute_node = random.sample(compute_nodes, 1)

        self.logger.info('Forwarding request to a compute node: ' +
                         compute_node[0])
        url = os.path.join('http://', compute_node[0], self.api_version, self.account)

        parsed, conn = http_connection(url)
        path = '%s/%s/%s' % (parsed.path, quote(self.container), quote(self.obj))

        return conn, path

    def _handle_get_through_compute_node(self):
        conn, path = self._prepare_connection()
        conn.request(self.method, path, None, self.req.headers)
        resp = conn.getresponse()

        def reader():
            try:
                return resp.read(65535)
            except (ValueError, IOError) as e:
                raise ValueError(str(e))

        data_source = iter(reader, '')

        response = Response(app_iter=data_source,
                            headers=conn.resp.headers,
                            request=self.req)

        return response

    def _handle_put_through_compute_node(self):
        conn, path = self._prepare_connection()
        data_source = self.req.environ['wsgi.input']
        resp = conn.putrequest(path, data_source, self.req.headers)
        response = Response(headers=resp.headers, request=self.req)

        return response

    @public
    def GET(self):
        """
        GET handler on Proxy
        """
        functions_data = self._get_functions()

        if functions_data:
            self.logger.info('There are functions to execute: ' +
                             str(functions_data))
            self.req.headers['functions_data'] = functions_data
            if self.disaggregated_compute:
                response = self._handle_get_through_compute_node()
            else:
                response = self.req.get_response(self.app)
        else:
            response = self.req.get_response(self.app)

        if 'Content-Length' not in response.headers and \
           'Transfer-Encoding' in response.headers:
            response.headers.pop('Transfer-Encoding')

        return response

    @public
    def PUT(self):
        """
        PUT handler on Proxy
        """
        functions_data = self._get_functions()

        if self.is_function_object_put:
            if not self._check_mandatory_metadata():
                msg = ('Mandatory function metadata not provided: ' +
                       str(self.mandatory_function_metadata) + '\n')
                raise HTTPUnauthorized(msg)

        elif functions_data:
            self.logger.info('There are functions to execute: ' +
                             str(functions_data))
            self.req.headers['functions_data'] = functions_data
            if self.disaggregated_compute:
                return self._handle_put_through_compute_node()
            else:
                return self.req.get_response(self.app)

        return self.req.get_response(self.app)

    @public
    def POST(self):
        """
        POST handler on Proxy
        """

        if self.is_function_set:
            response = self._set_function()
        elif self.is_function_unset:
            response = self._unset_function()
        else:
            response = self.req.get_response(self.app)

        return response

    @public
    def HEAD(self):
        """
        HEAD handler on Proxy
        """
        response = self.req.get_response(self.app)

        if self.conf['functions_visibility']:
            self._get_functions()
            if self.functions_list:
                for trigger in self.functions_list:
                    data = self.functions_list[trigger]
                    response.headers['Functions-'+trigger] = data

            if self.parent_functions_list:
                for trigger in self.parent_functions_list:
                    data = self.parent_functions_list[trigger]
                    response.headers['Functions-'+trigger+'-Container'] = data

        return response
