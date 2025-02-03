# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Protocol input serializes.

This module contains classes that implement input serialization
for the various AWS protocol types.

These classes essentially take user input, a model object that
represents what the expected input should look like, and it returns
a dictionary that contains the various parts of a request.  A few
high level design decisions:


* Each protocol type maps to a separate class, all inherit from
  ``Serializer``.
* The return value for ``serialize_to_request`` (the main entry
  point) returns a dictionary that represents a request.  This
  will have keys like ``url_path``, ``query_string``, etc.  This
  is done so that it's a) easy to test and b) not tied to a
  particular HTTP library.  See the ``serialize_to_request`` docstring
  for more details.

Unicode
-------

The input to the serializers should be text (str/unicode), not bytes,
with the exception of blob types.  Those are assumed to be binary,
and if a str/unicode type is passed in, it will be encoded as utf-8.
"""

import base64
import calendar
import datetime
import json
import re
import struct
import time
from xml.etree import ElementTree

from botocore import validate
from botocore.compat import formatdate
from botocore.exceptions import ParamValidationError
from botocore.utils import (
    has_header,
    is_json_value_header,
    parse_to_aware_datetime,
    percent_encode,
)
# From the spec, the default timestamp format if not specified is iso8601.
DEFAULT_TIMESTAMP_FORMAT = 'iso8601'
ISO8601 = '%Y-%m-%dT%H:%M:%SZ'
# Same as ISO8601, but with microsecond precision.
ISO8601_MICRO = '%Y-%m-%dT%H:%M:%S.%fZ'
HOST_PREFIX_RE = re.compile(r"^[A-Za-z0-9\.\-]+$")


def create_serializer(protocol_name, include_validation=True):
    # TODO: Unknown protocols.
    serializer = SERIALIZERS[protocol_name]()
    if include_validation:
        validator = validate.ParamValidator()
        serializer = validate.ParamValidationDecorator(validator, serializer)
    return serializer


class Serializer:
    DEFAULT_METHOD = 'POST'
    # Clients can change this to a different MutableMapping
    # (i.e OrderedDict) if they want.  This is used in the
    # compliance test to match the hash ordering used in the
    # tests.
    MAP_TYPE = dict
    DEFAULT_ENCODING = 'utf-8'

    def serialize_to_request(self, parameters, operation_model):
        """Serialize parameters into an HTTP request.

        This method takes user provided parameters and a shape
        model and serializes the parameters to an HTTP request.
        More specifically, this method returns information about
        parts of the HTTP request, it does not enforce a particular
        interface or standard for an HTTP request.  It instead returns
        a dictionary of:

            * 'url_path'
            * 'host_prefix'
            * 'query_string'
            * 'headers'
            * 'body'
            * 'method'

        It is then up to consumers to decide how to map this to a Request
        object of their HTTP library of choice.  Below is an example
        return value::

            {'body': {'Action': 'OperationName',
                      'Bar': 'val2',
                      'Foo': 'val1',
                      'Version': '2014-01-01'},
             'headers': {},
             'method': 'POST',
             'query_string': '',
             'host_prefix': 'value.',
             'url_path': '/'}

        :param parameters: The dictionary input parameters for the
            operation (i.e the user input).
        :param operation_model: The OperationModel object that describes
            the operation.
        """
        raise NotImplementedError("serialize_to_request")

    def _create_default_request(self):
        # Creates a boilerplate default request dict that subclasses
        # can use as a starting point.
        serialized = {
            'url_path': '/',
            'query_string': '',
            'method': self.DEFAULT_METHOD,
            'headers': {},
            # An empty body is represented as an empty byte string.
            'body': b'',
        }
        return serialized

    # Some extra utility methods subclasses can use.

    def _timestamp_iso8601(self, value):
        if value.microsecond > 0:
            timestamp_format = ISO8601_MICRO
        else:
            timestamp_format = ISO8601
        return value.strftime(timestamp_format)

    def _timestamp_unixtimestamp(self, value):
        return int(calendar.timegm(value.timetuple()))

    def _timestamp_rfc822(self, value):
        if isinstance(value, datetime.datetime):
            value = self._timestamp_unixtimestamp(value)
        return formatdate(value, usegmt=True)

    def _convert_timestamp_to_str(self, value, timestamp_format=None):
        if timestamp_format is None:
            timestamp_format = self.TIMESTAMP_FORMAT
        timestamp_format = timestamp_format.lower()
        datetime_obj = parse_to_aware_datetime(value)
        converter = getattr(self, f'_timestamp_{timestamp_format}')
        final_value = converter(datetime_obj)
        return final_value

    def _get_serialized_name(self, shape, default_name):
        # Returns the serialized name for the shape if it exists.
        # Otherwise it will return the passed in default_name.
        return shape.serialization.get('name', default_name)

    def _get_base64(self, value):
        # Returns the base64-encoded version of value, handling
        # both strings and bytes. The returned value is a string
        # via the default encoding.
        if isinstance(value, str):
            value = value.encode(self.DEFAULT_ENCODING)
        return base64.b64encode(value).strip().decode(self.DEFAULT_ENCODING)

    def _expand_host_prefix(self, parameters, operation_model):
        operation_endpoint = operation_model.endpoint
        if (
            operation_endpoint is None
            or 'hostPrefix' not in operation_endpoint
        ):
            return None

        host_prefix_expression = operation_endpoint['hostPrefix']
        input_members = operation_model.input_shape.members
        host_labels = [
            member
            for member, shape in input_members.items()
            if shape.serialization.get('hostLabel')
        ]
        format_kwargs = {}
        bad_labels = []
        for name in host_labels:
            param = parameters[name]
            if not HOST_PREFIX_RE.match(param):
                bad_labels.append(name)
            format_kwargs[name] = param
        if bad_labels:
            raise ParamValidationError(
                report=(
                    f"Invalid value for parameter(s): {', '.join(bad_labels)}. "
                    "Must contain only alphanumeric characters, hyphen, "
                    "or period."
                )
            )
        return host_prefix_expression.format(**format_kwargs)


class QuerySerializer(Serializer):
    TIMESTAMP_FORMAT = 'iso8601'

    def serialize_to_request(self, parameters, operation_model):
        shape = operation_model.input_shape
        serialized = self._create_default_request()
        serialized['method'] = operation_model.http.get(
            'method', self.DEFAULT_METHOD
        )
        serialized['headers'] = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'
        }
        # The query serializer only deals with body params so
        # that's what we hand off the _serialize_* methods.
        body_params = self.MAP_TYPE()
        body_params['Action'] = operation_model.name
        body_params['Version'] = operation_model.metadata['apiVersion']
        if shape is not None:
            self._serialize(body_params, parameters, shape)
        serialized['body'] = body_params

        host_prefix = self._expand_host_prefix(parameters, operation_model)
        if host_prefix is not None:
            serialized['host_prefix'] = host_prefix

        return serialized

    def _serialize(self, serialized, value, shape, prefix=''):
        # serialized: The dict that is incrementally added to with the
        #             final serialized parameters.
        # value: The current user input value.
        # shape: The shape object that describes the structure of the
        #        input.
        # prefix: The incrementally built up prefix for the serialized
        #         key (i.e Foo.bar.members.1).
        method = getattr(
            self,
            f'_serialize_type_{shape.type_name}',
            self._default_serialize,
        )
        method(serialized, value, shape, prefix=prefix)

    def _serialize_type_structure(self, serialized, value, shape, prefix=''):
        members = shape.members
        for key, value in value.items():
            member_shape = members[key]
            member_prefix = self._get_serialized_name(member_shape, key)
            if prefix:
                member_prefix = f'{prefix}.{member_prefix}'
            self._serialize(serialized, value, member_shape, member_prefix)

    def _serialize_type_list(self, serialized, value, shape, prefix=''):
        if not value:
            # The query protocol serializes empty lists.
            serialized[prefix] = ''
            return
        if self._is_shape_flattened(shape):
            list_prefix = prefix
            if shape.member.serialization.get('name'):
                name = self._get_serialized_name(shape.member, default_name='')
                # Replace '.Original' with '.{name}'.
                list_prefix = '.'.join(prefix.split('.')[:-1] + [name])
        else:
            list_name = shape.member.serialization.get('name', 'member')
            list_prefix = f'{prefix}.{list_name}'
        for i, element in enumerate(value, 1):
            element_prefix = f'{list_prefix}.{i}'
            element_shape = shape.member
            self._serialize(serialized, element, element_shape, element_prefix)

    def _serialize_type_map(self, serialized, value, shape, prefix=''):
        if self._is_shape_flattened(shape):
            full_prefix = prefix
        else:
            full_prefix = f'{prefix}.entry'
        template = full_prefix + '.{i}.{suffix}'
        key_shape = shape.key
        value_shape = shape.value
        key_suffix = self._get_serialized_name(key_shape, default_name='key')
        value_suffix = self._get_serialized_name(value_shape, 'value')
        for i, key in enumerate(value, 1):
            key_prefix = template.format(i=i, suffix=key_suffix)
            value_prefix = template.format(i=i, suffix=value_suffix)
            self._serialize(serialized, key, key_shape, key_prefix)
            self._serialize(serialized, value[key], value_shape, value_prefix)

    def _serialize_type_blob(self, serialized, value, shape, prefix=''):
        # Blob args must be base64 encoded.
        serialized[prefix] = self._get_base64(value)

    def _serialize_type_timestamp(self, serialized, value, shape, prefix=''):
        serialized[prefix] = self._convert_timestamp_to_str(
            value, shape.serialization.get('timestampFormat')
        )

    def _serialize_type_boolean(self, serialized, value, shape, prefix=''):
        if value:
            serialized[prefix] = 'true'
        else:
            serialized[prefix] = 'false'

    def _default_serialize(self, serialized, value, shape, prefix=''):
        serialized[prefix] = value

    def _is_shape_flattened(self, shape):
        return shape.serialization.get('flattened')


class EC2Serializer(QuerySerializer):
    """EC2 specific customizations to the query protocol serializers.

    The EC2 model is almost, but not exactly, similar to the query protocol
    serializer.  This class encapsulates those differences.  The model
    will have be marked with a ``protocol`` of ``ec2``, so you don't need
    to worry about wiring this class up correctly.

    """

    def _get_serialized_name(self, shape, default_name):
        # Returns the serialized name for the shape if it exists.
        # Otherwise it will return the passed in default_name.
        if 'queryName' in shape.serialization:
            return shape.serialization['queryName']
        elif 'name' in shape.serialization:
            # A locationName is always capitalized
            # on input for the ec2 protocol.
            name = shape.serialization['name']
            return name[0].upper() + name[1:]
        else:
            return default_name

    def _serialize_type_list(self, serialized, value, shape, prefix=''):
        for i, element in enumerate(value, 1):
            element_prefix = f'{prefix}.{i}'
            element_shape = shape.member
            self._serialize(serialized, element, element_shape, element_prefix)


class JSONSerializer(Serializer):
    TIMESTAMP_FORMAT = 'unixtimestamp'

    def serialize_to_request(self, parameters, operation_model):
        target = '{}.{}'.format(
            operation_model.metadata['targetPrefix'],
            operation_model.name,
        )
        json_version = operation_model.metadata['jsonVersion']
        serialized = self._create_default_request()
        serialized['method'] = operation_model.http.get(
            'method', self.DEFAULT_METHOD
        )
        serialized['headers'] = {
            'X-Amz-Target': target,
            'Content-Type': f'application/x-amz-json-{json_version}',
        }
        body = self.MAP_TYPE()
        input_shape = operation_model.input_shape
        if input_shape is not None:
            self._serialize(body, parameters, input_shape)
        serialized['body'] = json.dumps(body).encode(self.DEFAULT_ENCODING)

        host_prefix = self._expand_host_prefix(parameters, operation_model)
        if host_prefix is not None:
            serialized['host_prefix'] = host_prefix

        return serialized

    def _serialize(self, serialized, value, shape, key=None):
        method = getattr(
            self,
            f'_serialize_type_{shape.type_name}',
            self._default_serialize,
        )
        method(serialized, value, shape, key)

    def _serialize_type_structure(self, serialized, value, shape, key):
        if shape.is_document_type:
            serialized[key] = value
        else:
            if key is not None:
                # If a key is provided, this is a result of a recursive
                # call so we need to add a new child dict as the value
                # of the passed in serialized dict.  We'll then add
                # all the structure members as key/vals in the new serialized
                # dictionary we just created.
                new_serialized = self.MAP_TYPE()
                serialized[key] = new_serialized
                serialized = new_serialized
            members = shape.members
            for member_key, member_value in value.items():
                member_shape = members[member_key]
                if 'name' in member_shape.serialization:
                    member_key = member_shape.serialization['name']
                self._serialize(
                    serialized, member_value, member_shape, member_key
                )

    def _serialize_type_map(self, serialized, value, shape, key):
        map_obj = self.MAP_TYPE()
        serialized[key] = map_obj
        for sub_key, sub_value in value.items():
            self._serialize(map_obj, sub_value, shape.value, sub_key)

    def _serialize_type_list(self, serialized, value, shape, key):
        list_obj = []
        serialized[key] = list_obj
        for list_item in value:
            wrapper = {}
            # The JSON list serialization is the only case where we aren't
            # setting a key on a dict.  We handle this by using
            # a __current__ key on a wrapper dict to serialize each
            # list item before appending it to the serialized list.
            self._serialize(wrapper, list_item, shape.member, "__current__")
            list_obj.append(wrapper["__current__"])

    def _default_serialize(self, serialized, value, shape, key):
        serialized[key] = value

    def _serialize_type_timestamp(self, serialized, value, shape, key):
        serialized[key] = self._convert_timestamp_to_str(
            value, shape.serialization.get('timestampFormat')
        )

    def _serialize_type_blob(self, serialized, value, shape, key):
        serialized[key] = self._get_base64(value)


class BaseRestSerializer(Serializer):
    """Base class for rest protocols.

    The only variance between the various rest protocols is the
    way that the body is serialized.  All other aspects (headers, uri, etc.)
    are the same and logic for serializing those aspects lives here.

    Subclasses must implement the ``_serialize_body_params`` method.

    """

    QUERY_STRING_TIMESTAMP_FORMAT = 'iso8601'
    HEADER_TIMESTAMP_FORMAT = 'rfc822'
    # This is a list of known values for the "location" key in the
    # serialization dict.  The location key tells us where on the request
    # to put the serialized value.
    KNOWN_LOCATIONS = ['uri', 'querystring', 'header', 'headers']

    def serialize_to_request(self, parameters, operation_model):
        serialized = self._create_default_request()
        serialized['method'] = operation_model.http.get(
            'method', self.DEFAULT_METHOD
        )
        shape = operation_model.input_shape
        if shape is None:
            serialized['url_path'] = operation_model.http['requestUri']
            return serialized
        shape_members = shape.members
        # While the ``serialized`` key holds the final serialized request
        # data, we need interim dicts for the various locations of the
        # request.  We need this for the uri_path_kwargs and the
        # query_string_kwargs because they are templated, so we need
        # to gather all the needed data for the string template,
        # then we render the template.  The body_kwargs is needed
        # because once we've collected them all, we run them through
        # _serialize_body_params, which for rest-json, creates JSON,
        # and for rest-xml, will create XML.  This is what the
        # ``partitioned`` dict below is for.
        partitioned = {
            'uri_path_kwargs': self.MAP_TYPE(),
            'query_string_kwargs': self.MAP_TYPE(),
            'body_kwargs': self.MAP_TYPE(),
            'headers': self.MAP_TYPE(),
        }
        for param_name, param_value in parameters.items():
            if param_value is None:
                # Don't serialize any parameter with a None value.
                continue
            self._partition_parameters(
                partitioned, param_name, param_value, shape_members
            )
        serialized['url_path'] = self._render_uri_template(
            operation_model.http['requestUri'], partitioned['uri_path_kwargs']
        )

        if 'authPath' in operation_model.http:
            serialized['auth_path'] = self._render_uri_template(
                operation_model.http['authPath'],
                partitioned['uri_path_kwargs'],
            )
        # Note that we lean on the http implementation to handle the case
        # where the requestUri path already has query parameters.
        # The bundled http client, requests, already supports this.
        serialized['query_string'] = partitioned['query_string_kwargs']
        if partitioned['headers']:
            serialized['headers'] = partitioned['headers']
        self._serialize_payload(
            partitioned, parameters, serialized, shape, shape_members
        )
        self._serialize_content_type(serialized, shape, shape_members)

        host_prefix = self._expand_host_prefix(parameters, operation_model)
        if host_prefix is not None:
            serialized['host_prefix'] = host_prefix

        return serialized

    def _render_uri_template(self, uri_template, params):
        # We need to handle two cases::
        #
        # /{Bucket}/foo
        # /{Key+}/bar
        # A label ending with '+' is greedy.  There can only
        # be one greedy key.
        encoded_params = {}
        for template_param in re.findall(r'{(.*?)}', uri_template):
            if template_param.endswith('+'):
                encoded_params[template_param] = percent_encode(
                    params[template_param[:-1]], safe='/~'
                )
            else:
                encoded_params[template_param] = percent_encode(
                    params[template_param]
                )
        return uri_template.format(**encoded_params)

    def _serialize_payload(
        self, partitioned, parameters, serialized, shape, shape_members
    ):
        # partitioned - The user input params partitioned by location.
        # parameters - The user input params.
        # serialized - The final serialized request dict.
        # shape - Describes the expected input shape
        # shape_members - The members of the input struct shape
        payload_member = shape.serialization.get('payload')
        if self._has_streaming_payload(payload_member, shape_members):
            # If it's streaming, then the body is just the
            # value of the payload.
            body_payload = parameters.get(payload_member, b'')
            body_payload = self._encode_payload(body_payload)
            serialized['body'] = body_payload
        elif payload_member is not None:
            # If there's a payload member, we serialized that
            # member to they body.
            body_params = parameters.get(payload_member)
            if body_params is not None:
                serialized['body'] = self._serialize_body_params(
                    body_params, shape_members[payload_member]
                )
            else:
                serialized['body'] = self._serialize_empty_body()
        elif partitioned['body_kwargs']:
            serialized['body'] = self._serialize_body_params(
                partitioned['body_kwargs'], shape
            )
        elif self._requires_empty_body(shape):
            serialized['body'] = self._serialize_empty_body()

    def _serialize_empty_body(self):
        return b''

    def _serialize_content_type(self, serialized, shape, shape_members):
        """
        Some protocols require varied Content-Type headers
        depending on user input. This allows subclasses to apply
        this conditionally.
        """
        pass

    def _requires_empty_body(self, shape):
        """
        Some protocols require a specific body to represent an empty
        payload. This allows subclasses to apply this conditionally.
        """
        return False

    def _has_streaming_payload(self, payload, shape_members):
        """Determine if payload is streaming (a blob or string)."""
        return payload is not None and shape_members[payload].type_name in (
            'blob',
            'string',
        )

    def _encode_payload(self, body):
        if isinstance(body, str):
            return body.encode(self.DEFAULT_ENCODING)
        return body

    def _partition_parameters(
        self, partitioned, param_name, param_value, shape_members
    ):
        # This takes the user provided input parameter (``param``)
        # and figures out where they go in the request dict.
        # Some params are HTTP headers, some are used in the URI, some
        # are in the request body.  This method deals with this.
        member = shape_members[param_name]
        location = member.serialization.get('location')
        key_name = member.serialization.get('name', param_name)
        if location == 'uri':
            partitioned['uri_path_kwargs'][key_name] = param_value
        elif location == 'querystring':
            if isinstance(param_value, dict):
                partitioned['query_string_kwargs'].update(param_value)
            elif isinstance(param_value, bool):
                bool_str = str(param_value).lower()
                partitioned['query_string_kwargs'][key_name] = bool_str
            elif member.type_name == 'timestamp':
                timestamp_format = member.serialization.get(
                    'timestampFormat', self.QUERY_STRING_TIMESTAMP_FORMAT
                )
                timestamp = self._convert_timestamp_to_str(
                    param_value, timestamp_format
                )
                partitioned['query_string_kwargs'][key_name] = timestamp
            else:
                partitioned['query_string_kwargs'][key_name] = param_value
        elif location == 'header':
            shape = shape_members[param_name]
            if not param_value and shape.type_name == 'list':
                # Empty lists should not be set on the headers
                return
            value = self._convert_header_value(shape, param_value)
            partitioned['headers'][key_name] = str(value)
        elif location == 'headers':
            # 'headers' is a bit of an oddball.  The ``key_name``
            # is actually really a prefix for the header names:
            header_prefix = key_name
            # The value provided by the user is a dict so we'll be
            # creating multiple header key/val pairs.  The key
            # name to use for each header is the header_prefix (``key_name``)
            # plus the key provided by the user.
            self._do_serialize_header_map(
                header_prefix, partitioned['headers'], param_value
            )
        else:
            partitioned['body_kwargs'][param_name] = param_value

    def _do_serialize_header_map(self, header_prefix, headers, user_input):
        for key, val in user_input.items():
            full_key = header_prefix + key
            headers[full_key] = val

    def _serialize_body_params(self, params, shape):
        raise NotImplementedError('_serialize_body_params')

    def _convert_header_value(self, shape, value):
        if shape.type_name == 'timestamp':
            datetime_obj = parse_to_aware_datetime(value)
            timestamp = calendar.timegm(datetime_obj.utctimetuple())
            timestamp_format = shape.serialization.get(
                'timestampFormat', self.HEADER_TIMESTAMP_FORMAT
            )
            return self._convert_timestamp_to_str(timestamp, timestamp_format)
        elif shape.type_name == 'list':
            converted_value = [
                self._convert_header_value(shape.member, v)
                for v in value
                if v is not None
            ]
            return ",".join(converted_value)
        elif is_json_value_header(shape):
            # Serialize with no spaces after separators to save space in
            # the header.
            return self._get_base64(json.dumps(value, separators=(',', ':')))
        else:
            return value


class RestJSONSerializer(BaseRestSerializer, JSONSerializer):
    def _serialize_empty_body(self):
        return b'{}'

    def _requires_empty_body(self, shape):
        """
        Serialize an empty JSON object whenever the shape has
        members not targeting a location.
        """
        for member, val in shape.members.items():
            if 'location' not in val.serialization:
                return True
        return False

    def _serialize_content_type(self, serialized, shape, shape_members):
        """Set Content-Type to application/json for all structured bodies."""
        payload = shape.serialization.get('payload')
        if self._has_streaming_payload(payload, shape_members):
            # Don't apply content-type to streaming bodies
            return

        has_body = serialized['body'] != b''
        has_content_type = has_header('Content-Type', serialized['headers'])
        if has_body and not has_content_type:
            serialized['headers']['Content-Type'] = 'application/json'

    def _serialize_body_params(self, params, shape):
        serialized_body = self.MAP_TYPE()
        self._serialize(serialized_body, params, shape)
        return json.dumps(serialized_body).encode(self.DEFAULT_ENCODING)


class RestXMLSerializer(BaseRestSerializer):
    TIMESTAMP_FORMAT = 'iso8601'

    def _serialize_body_params(self, params, shape):
        root_name = shape.serialization['name']
        pseudo_root = ElementTree.Element('')
        self._serialize(shape, params, pseudo_root, root_name)
        real_root = list(pseudo_root)[0]
        return ElementTree.tostring(real_root, encoding=self.DEFAULT_ENCODING)

    def _serialize(self, shape, params, xmlnode, name):
        method = getattr(
            self,
            f'_serialize_type_{shape.type_name}',
            self._default_serialize,
        )
        method(xmlnode, params, shape, name)

    def _serialize_type_structure(self, xmlnode, params, shape, name):
        structure_node = ElementTree.SubElement(xmlnode, name)

        if 'xmlNamespace' in shape.serialization:
            namespace_metadata = shape.serialization['xmlNamespace']
            attribute_name = 'xmlns'
            if namespace_metadata.get('prefix'):
                attribute_name += f":{namespace_metadata['prefix']}"
            structure_node.attrib[attribute_name] = namespace_metadata['uri']
        for key, value in params.items():
            member_shape = shape.members[key]
            member_name = member_shape.serialization.get('name', key)
            # We need to special case member shapes that are marked as an
            # xmlAttribute.  Rather than serializing into an XML child node,
            # we instead serialize the shape to an XML attribute of the
            # *current* node.
            if value is None:
                # Don't serialize any param whose value is None.
                return
            if member_shape.serialization.get('xmlAttribute'):
                # xmlAttributes must have a serialization name.
                xml_attribute_name = member_shape.serialization['name']
                structure_node.attrib[xml_attribute_name] = value
                continue
            self._serialize(member_shape, value, structure_node, member_name)

    def _serialize_type_list(self, xmlnode, params, shape, name):
        member_shape = shape.member
        if shape.serialization.get('flattened'):
            element_name = name
            list_node = xmlnode
        else:
            element_name = member_shape.serialization.get('name', 'member')
            list_node = ElementTree.SubElement(xmlnode, name)
        for item in params:
            self._serialize(member_shape, item, list_node, element_name)

    def _serialize_type_map(self, xmlnode, params, shape, name):
        # Given the ``name`` of MyMap, and input of {"key1": "val1"}
        # we serialize this as:
        #   <MyMap>
        #     <entry>
        #       <key>key1</key>
        #       <value>val1</value>
        #     </entry>
        #  </MyMap>
        node = ElementTree.SubElement(xmlnode, name)
        # TODO: handle flattened maps.
        for key, value in params.items():
            entry_node = ElementTree.SubElement(node, 'entry')
            key_name = self._get_serialized_name(shape.key, default_name='key')
            val_name = self._get_serialized_name(
                shape.value, default_name='value'
            )
            self._serialize(shape.key, key, entry_node, key_name)
            self._serialize(shape.value, value, entry_node, val_name)

    def _serialize_type_boolean(self, xmlnode, params, shape, name):
        # For scalar types, the 'params' attr is actually just a scalar
        # value representing the data we need to serialize as a boolean.
        # It will either be 'true' or 'false'
        node = ElementTree.SubElement(xmlnode, name)
        if params:
            str_value = 'true'
        else:
            str_value = 'false'
        node.text = str_value

    def _serialize_type_blob(self, xmlnode, params, shape, name):
        node = ElementTree.SubElement(xmlnode, name)
        node.text = self._get_base64(params)

    def _serialize_type_timestamp(self, xmlnode, params, shape, name):
        node = ElementTree.SubElement(xmlnode, name)
        node.text = self._convert_timestamp_to_str(
            params, shape.serialization.get('timestampFormat')
        )

    def _default_serialize(self, xmlnode, params, shape, name):
        node = ElementTree.SubElement(xmlnode, name)
        node.text = str(params)

    # class RpcCBORSerializer:
    #     MAJOR_TYPE_UNSIGNED_INT = 0x00  # 000_00000 - Major Type 0 - unsigned int
    #     MAJOR_TYPE_NEGATIVE_INT = 0x20  # 001_00000 - Major Type 1 - negative int
    #     MAJOR_TYPE_BYTE_STR = 0x40  # 010_00000 - Major Type 2 (Byte String)
    #     MAJOR_TYPE_STR = 0x60  # 011_00000 - Major Type 3 (Text String)
    #     MAJOR_TYPE_ARRAY = 0x80  # 100_00000 - Major Type 4 (Array)
    #     MAJOR_TYPE_MAP = 0xa0  # 101_00000 - Major Type 5 (Map)
    #     MAJOR_TYPE_TAG = 0xc0  # 110_00000 - Major type 6 (Tag)
    #     MAJOR_TYPE_SIMPLE = 0xe0  # 111_00000 - Major type 7 (111) + 5 bit 0

class CBORSerializer(Serializer):
    MAJOR_TYPE_UNSIGNED_INT = 0x00  # 000_00000 - Major Type 0 - unsigned int
    MAJOR_TYPE_NEGATIVE_INT = 0x20  # 001_00000 - Major Type 1 - negative int
    MAJOR_TYPE_BYTE_STR = 0x40  # 010_00000 - Major Type 2 (Byte String)
    MAJOR_TYPE_STR = 0x60  # 011_00000 - Major Type 3 (Text String)
    MAJOR_TYPE_ARRAY = 0x80  # 100_00000 - Major Type 4 (Array)
    MAJOR_TYPE_MAP = b'0xa0'  # 101_00000 - Major Type 5 (Map)
    MAJOR_TYPE_TAG = 0xc0  # 110_00000 - Major type 6 (Tag)
    MAJOR_TYPE_SIMPLE = 0xe0  # 111_00000 - Major type 7 (111) + 5 bit 0

    def __init__(self):
        self._time_spent = 0
        self._type_to_method = {
            'int': self._serialize_type_int,
            'float': self._serialize_type_float,
            'string': self._serialize_type_string,
            'array': self._serialize_type_array,
            'map': self._serialize_type_map,
            'structure': self._serialize_type_structure,
        }

    def serialize_to_request(self, parameters, operation_model):
        serialized = self._create_default_request()
        serialized['method'] = 'POST'  # CBOR
        serialized['headers'] = {
            'smithy-protocol': 'rpc-v2-cbor',
            'Content-Type': 'application/cbor', #TODO this MUST go in only if we
            # serialize a body
        }
        service_name = operation_model.service_model.metadata['targetPrefix']
        operation_name = operation_model.name
        serialized['url_path'] = f'/service/{service_name}/operation/{operation_name}'
        body = bytearray()
        input_shape = operation_model.input_shape
        if input_shape is not None:
            self._serialize_data_item(body, parameters, input_shape)
            serialized['body'] = body
        print(f"{self._time_spent:.10f}")
        return serialized

    def _serialize_data_item(self, serialized, value, shape, key=None):
        start_time = time.time()
        method = self._type_to_method.get(shape.type_name, self._default_serialize)
        end_time = time.time()
        self._time_spent += (end_time - start_time)
        if key is not None:
            print(key)
        method(serialized, value, shape, key)


    def _serialize_type_int(self, serialized, value, shape, key):
        if value >= 0:
            if value < 24:
                serialized.extend(bytes([value]))
            elif value < 256:
                serialized.extend(b'\x18' + value.to_bytes(1, "big"))
            elif value < 65536:
                serialized.extend(b'\x19' + value.to_bytes(2, "big"))
            elif value < 4294967296:
                serialized.extend(b'\x1a' + value.to_bytes(4, "big"))
            else:
                serialized.extend(b'\x1b' + value.to_bytes(8, "big"))
        else:
            value = -1 - value
            if value < 24:
                serialized.extend(bytes([0x20 + value]))
            elif value < 256:
                serialized.extend(b'\x38' + value.to_bytes(1, "big"))
            elif value < 65536:
                serialized.extend(b'\x39' + value.to_bytes(2, "big"))
            elif value < 4294967296:
                serialized.extend(b'\x3a' + value.to_bytes(4, "big"))
            else:
                serialized.extend(b'\x3b' + value.to_bytes(8, "big"))

    def _serialize_type_float(self, serialized, value, shape, key):
        # Serialize float as double-precision (major type 7, additional information 27)
        serialized.extend(b'\xfb' + struct.pack(">d", value))

    def _serialize_type_string(self, serialized, value, shape, key):
        encoded = value.encode('utf-8')
        length = len(encoded)
        if length < 24:
            serialized.extend(bytes([0x60 + length]) + encoded)
        elif length < 256:
            serialized.extend(b'\x78' + length.to_bytes(1, 'big') + encoded)
        elif length < 65536:
            serialized.extend(b'\x79' + length.to_bytes(2, 'big') + encoded)
        elif length < 4294967296:
            serialized.extend(b'\x7a' + length.to_bytes(4, 'big') + encoded)
        else:
            serialized.extend(b'\x7b' + length.to_bytes(8, 'big') + encoded)


    def _serialize_type_structure(self, serialized, value, shape, key):
        if key is not None:
            # For nested structures, we need to serialize the key first
            self._serialize_data_item(serialized, key, shape.key_shape)
        # Start the map serialization
        members = shape.members
        map_length = len(value)
        if map_length < 24:
            serialized.extend(struct.pack('B', 0xa0 + map_length))
        elif map_length < 256:
            serialized.extend(b'\xb8' + struct.pack('B', map_length))
        elif map_length < 65536:
            serialized.extend(b'\xb9' + struct.pack('>H', map_length))
        elif map_length < 4294967296:
            serialized.extend(b'\xba' + struct.pack('>I', map_length))
        else:
            serialized.extend(b'\xbb' + struct.pack('>Q', map_length))
        for member_key, member_value in value.items():
            member_shape = members[member_key]
            if 'name' in member_shape.serialization:
                member_key = member_shape.serialization['name']
            self._serialize_type_string(serialized, member_key, None, None)
            self._serialize_data_item(serialized, member_value, member_shape)

    def _serialize_type_array(self, serialized, value, shape, key):
        length = len(value)
        if length < 24:
            serialized.extend(struct.pack('B', 0x80 + length))
        elif length < 256:
            serialized.extend(b'\x98' + struct.pack('B', length))
        elif length < 65536:
            serialized.extend(b'\x99' + struct.pack('>H', length))
        elif length < 4294967296:
            serialized.extend(b'\x9a' + struct.pack('>I', length))
        else:
            serialized.extend(b'\x9b' + struct.pack('>Q', length))
        for item in value:
            self._serialize_data_item(serialized, item, shape.member)

    def _serialize_type_map(self, serialized, value, shape, key):
        length = len(value)
        if length < 24:
            serialized.extend(struct.pack('B', 0xa0 + length))
        elif length < 256:
            serialized.extend(b'\xb8' + struct.pack('B', length))
        elif length < 65536:
            serialized.extend(b'\xb9' + struct.pack('>H', length))
        elif length < 4294967296:
            serialized.extend(b'\xba' + struct.pack('>I', length))
        else:
            serialized.extend(b'\xbb' + struct.pack('>Q', length))
        for key_item, item in value.items():
            self._serialize_data_item(serialized, key_item, shape.key)
            self._serialize_data_item(serialized, item, shape.value)

    def _default_serialize(self, serialized, value, shape, key):
        # Default serialization (e.g., fallback to string serialization)
        encoded = str(value).encode('utf-8')
        length = len(encoded)
        if length < 24:
            serialized.extend(bytes([0x60 + length]) + encoded)
        elif length < 256:
            serialized.extend(b'\x78' + length.to_bytes(1, 'big') + encoded)
        elif length < 65536:
            serialized.extend(b'\x79' + length.to_bytes(2, 'big') + encoded)
        elif length < 4294967296:
            serialized.extend(b'\x7a' + length.to_bytes(4, 'big') + encoded)
        else:
            serialized.extend(b'\x7b' + length.to_bytes(8, 'big') + encoded)
    #
    # def _serialize(self, data):
    #     if data is None:
    #         return b'\xf6'  # CBOR null
    #     elif isinstance(data, bool):
    #         return b'\xf5' if data else b'\xf4'  # CBOR true/false
    #     elif isinstance(data, int):
    #         return self._serialize_int(data)
    #     elif isinstance(data, float):
    #         return self._serialize_float(data)
    #     elif isinstance(data, str):
    #         return self._serialize_string(data)
    #     elif isinstance(data, list):
    #         return self._serialize_array(data)
    #     elif isinstance(data, dict):
    #         return self._serialize_map(data)
    #     else:
    #         raise TypeError(f"Unsupported type: {type(data)}")
    #
    # def _serialize_int(self, value):
    #     if value >= 0:
    #         if value < 24:
    #             return bytes([value])
    #         elif value < 256:
    #             return b'\x18' + value.to_bytes(1, "big")
    #         elif value < 65536:
    #             return b'\x19' + value.to_bytes(2, "big")
    #         elif value < 4294967296:
    #             return b'\x1a' + value.to_bytes(4, "big")
    #         else:
    #             return b'\x1b' + value.to_bytes(8, "big")
    #     else:
    #         value = -1 - value
    #         if value < 24:
    #             return bytes([0x20 + value])
    #         elif value < 256:
    #             return b'\x38' + value.to_bytes(1, "big")
    #         elif value < 65536:
    #             return b'\x39' + value.to_bytes(2, "big")
    #         elif value < 4294967296:
    #             return b'\x3a' + value.to_bytes(4, "big")
    #         else:
    #             return b'\x3b' + value.to_bytes(8, "big")
    #
    # def _serialize_float(self, value):
    #     import struct
    #     # Serialize float as double-precision (major type 7, additional information 27)
    #     return b'\xfb' + struct.pack(">d", value)
    #
    # def _serialize_string(self, value):
    #     #TODO separate this out
    #     encoded = value.encode('utf-8')
    #     length = len(encoded)
    #     78
    #     if length < 24:
    #         return bytes([0x60 + length]) + encoded
    #     elif length < 256:
    #         return b'\x78' + length.to_bytes(1, 'big') + encoded
    #     elif length < 65536:
    #         return b'\x79' + length.to_bytes(2, 'big') + encoded
    #     elif length < 4294967296:
    #         return b'\x7a' + length.to_bytes(4, 'big') + encoded
    #     else:
    #         return b'\x7b' + length.to_bytes(8, 'big') + encoded
    #
    # def _serialize_array(self, value):
    #     length = len(value)
    #     result = bytearray()
    #     if length < 24:
    #         result.extend(struct.pack('B', 0x80 + length))
    #     elif length < 256:
    #         result.extend(b'\x98' + struct.pack('B', length))
    #     elif length < 65536:
    #         result.extend(b'\x99' + struct.pack('>H', length))
    #     elif length < 4294967296:
    #         result.extend(b'\x9a' + struct.pack('>I', length))
    #     else:
    #         result.extend(b'\x9b' + struct.pack('>Q', length))
    #     for item in value:
    #         result.extend(self._serialize(item))
    #     return result
    #
    # def _serialize_map(self, value):
    #     length = len(value)
    #     result = bytearray()
    #     if length < 24:
    #         result.extend(struct.pack('B', 0xa0 + length))
    #     elif length < 256:
    #         result.extend(b'\xb8' + struct.pack('B', length))
    #     elif length < 65536:
    #         result.extend(b'\xb9' + struct.pack('>H', length))
    #     elif length < 4294967296:
    #         result.extend(b'\xba' + struct.pack('>I', length))
    #     else:
    #         result.extend(b'\xbb' + struct.pack('>Q', length))
    #     for key, item in value.items():
    #         result.extend(self._serialize(key))
    #         result.extend(self._serialize(item))
    #     return result


from awscrt.cbor import AwsCborEncoder
class CRTCBORSerializer(Serializer):

    def serialize_to_request(self, parameters, operation_model):
        serialized = self._create_default_request()
        serialized['method'] = operation_model.http.get(
            'method', self.DEFAULT_METHOD
        )
        serialized['headers'] = {
            'smithy-protocol': 'rpc-v2-cbor',
            'Content-Type': f'application/cbor',
        }
        service_name = operation_model.service_model.metadata['targetPrefix']
        operation_name = operation_model.name
        serialized['url_path'] = f'/service/{service_name}/operation/{operation_name}'
        input_shape = operation_model.input_shape
        if input_shape is not None:
            serialized['body'] = self._serialize(parameters)
        return serialized

    def _serialize(self, data):
        cbor_encoder = AwsCborEncoder()
        cbor_encoder.write_data_item(data)
        return cbor_encoder.get_encoded_data()


class CborSerializer2:

    def __init__(self):
        self.bytesarr = bytearray()

    def serialize_to_request(self, parameters, operation_model):
        serialized = self._create_default_request()
        serialized['method'] = 'POST'  # CBOR
        serialized['headers'] = {
            'smithy-protocol': 'rpc-v2-cbor',
            'Content-Type': 'application/cbor',  # TODO this MUST go in only if we
            # serialize a body
        }
        service_name = operation_model.service_model.metadata['targetPrefix']
        operation_name = operation_model.name
        serialized['url_path'] = f'/service/{service_name}/operation/{operation_name}'
        input_shape = operation_model.input_shape
        self.bytesarr.clear()
        if input_shape is not None:
            self._serialize(parameters)
            serialized['body'] = self.bytesarr
        return serialized

    def _serialize(self, data):
        if data is None:
            self.bytesarr.extend(b'\xf6')  # CBOR null
        elif isinstance(data, bool):
            self.bytesarr.extend(b'\xf5' if data else b'\xf4')  # CBOR true/false
        elif isinstance(data, int):
            self._serialize_int(data)
        elif isinstance(data, float):
            self._serialize_float(data)
        elif isinstance(data, str):
            self._serialize_string(data)
        elif isinstance(data, list):
            self._serialize_array(data)
        elif isinstance(data, dict):
            self._serialize_map(data)
        else:
            raise TypeError(f"Unsupported type: {type(data)}")

    def _serialize_int(self, value):
        if value >= 0:
            if value < 24:
                self.bytesarr.extend(bytes([value]))
            elif value < 256:
                self.bytesarr.extend(b'\x18' + value.to_bytes(1, "big"))
            elif value < 65536:
                self.bytesarr.extend(b'\x19' + value.to_bytes(2, "big"))
            elif value < 4294967296:
                self.bytesarr.extend(b'\x1a' + value.to_bytes(4, "big"))
            else:
                self.bytesarr.extend(b'\x1b' + value.to_bytes(8, "big"))
        else:
            value = -1 - value
            if value < 24:
                self.bytesarr.extend(bytes([0x20 + value]))
            elif value < 256:
                self.bytesarr.extend(b'\x38' + value.to_bytes(1, "big"))
            elif value < 65536:
                self.bytesarr.extend(b'\x39' + value.to_bytes(2, "big"))
            elif value < 4294967296:
                self.bytesarr.extend(b'\x3a' + value.to_bytes(4, "big"))
            else:
                self.bytesarr.extend(b'\x3b' + value.to_bytes(8, "big"))

    def _serialize_float(self, value):
        # Serialize float as double-precision (major type 7, additional information 27)
        self.bytesarr.extend(b'\xfb' + struct.pack(">d", value))

    def _serialize_string(self, value):
        encoded = value.encode('utf-8')
        length = len(encoded)
        if length < 24:
            self.bytesarr.extend(bytes([0x60 + length]) + encoded)
        elif length < 256:
            self.bytesarr.extend(b'\x78' + length.to_bytes(1, 'big') + encoded)
        elif length < 65536:
            self.bytesarr.extend(b'\x79' + length.to_bytes(2, 'big') + encoded)
        elif length < 4294967296:
            self.bytesarr.extend(b'\x7a' + length.to_bytes(4, 'big') + encoded)
        else:
            self.bytesarr.extend(b'\x7b' + length.to_bytes(8, 'big') + encoded)

    def _serialize_array(self, value):
        length = len(value)
        if length < 24:
            self.bytesarr.extend(struct.pack('B', 0x80 + length))
        elif length < 256:
            self.bytesarr.extend(b'\x98' + struct.pack('B', length))
        elif length < 65536:
            self.bytesarr.extend(b'\x99' + struct.pack('>H', length))
        elif length < 4294967296:
            self.bytesarr.extend(b'\x9a' + struct.pack('>I', length))
        else:
            self.bytesarr.extend(b'\x9b' + struct.pack('>Q', length))
        for item in value:
            self._serialize(item)

    def _serialize_map(self, value):
        length = len(value)
        if length < 24:
            self.bytesarr.extend(struct.pack('B', 0xa0 + length))
        elif length < 256:
            self.bytesarr.extend(b'\xb8' + struct.pack('B', length))
        elif length < 65536:
            self.bytesarr.extend(b'\xb9' + struct.pack('>H', length))
        elif length < 4294967296:
            self.bytesarr.extend(b'\xba' + struct.pack('>I', length))
        else:
            self.bytesarr.extend(b'\xbb' + struct.pack('>Q', length))
        for key, item in value.items():
            self._serialize(key)
            self._serialize(item)

SERIALIZERS = {
    'ec2': EC2Serializer,
    'query': QuerySerializer,
    'json': JSONSerializer,
    'rest-json': RestJSONSerializer,
    'rest-xml': RestXMLSerializer,
    'smithy-rpc-v2-cbor': CBORSerializer,
    'memoryview': CBORSerializer,
    'crtcbor': CRTCBORSerializer,
}
