from wsgiref.util import FileWrapper

from django.http import StreamingHttpResponse, HttpResponse
from werkzeug.http import parse_range_header

from corehq.util.files import safe_filename_header


class RangedFileWrapper:
    """
    Wraps a file like object with an iterator that runs over part (or all) of
    the file defined by start and stop. Blocks of block_size will be returned
    from the starting position, up to, but not including the stop point.

    Based off: https://github.com/satchamo/django/commit/2ce75c5c4bee2a858c0214d136bfcd351fcde11d
    """
    block_size = 8192

    def __init__(self, filelike, start=0, stop=float("inf"), block_size=None):
        self.filelike = filelike
        self.block_size = block_size or RangedFileWrapper.block_size
        self.start = start
        self.stop = stop
        if hasattr(filelike, 'close'):
            self.close = filelike.close

    def __iter__(self):
        if hasattr(self.filelike, 'seek'):
            self.filelike.seek(self.start)
        else:
            pos_tmp = 0
            while pos_tmp < self.start:
                data = self.filelike.read(min(self.block_size, self.start - pos_tmp))
                pos_tmp += len(data)
                if not data or pos_tmp == self.start:
                    break

        position = self.start
        while position < self.stop:
            data = self.filelike.read(min(self.block_size, self.stop - position))
            if not data:
                break

            yield data
            position += self.block_size


def get_download_response(payload, content_length, content_format, filename, request=None):
    """
    :param payload: File like object.
    :param content_length: Size of payload in bytes
    :param content_format: ``couchexport.models.Format`` instance
    :param filename: Name of the download
    :param request: The request. Used to determine if a range response should be given.
    :return: HTTP response
    """
    ranges = None
    if "HTTP_RANGE" in request.META:
        try:
            ranges = parse_range_header(request.META['HTTP_RANGE'], content_length)
        except ValueError:
            pass

    if ranges is None or len(ranges.ranges) != 1:
        ranges = None

    if ranges:
        response_file = RangedFileWrapper(payload)
    else:
        response_file = FileWrapper(payload)

    response = StreamingHttpResponse(response_file, content_type=content_format.mimetype)
    if content_format.download:
        response['Content-Disposition'] = safe_filename_header(filename)

    response["Content-Length"] = content_length
    response["Accept-Ranges"] = "bytes"

    if ranges:
        start, stop = ranges.ranges[0]
        if stop is not None and stop > content_length:
            # requested range not satisfiable
            return HttpResponse(status=416)
        response_file.start = start
        if stop:
            response_file.stop = stop
        end = stop - 1 if stop else content_length
        response["Content-Range"] = "bytes %d-%d/%d" % (start, end, content_length)
        response["Content-Length"] = (stop or content_length) - start
        response.status_code = 206
    return response
