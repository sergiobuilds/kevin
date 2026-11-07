import base64
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from PIL import Image
import pixels

class PixelTests(unittest.TestCase):
    def test_generation_animation_and_edit(self):
        s=pixels.generate('눈 내리는 크리스마스 마을')
        self.assertEqual((len(s['frames']),len(s['frames'][0]),len(s['frames'][0][0])),(13,16,144))
        self.assertNotEqual(s['frames'][0],s['frames'][1])
        before=s['frames'][0][12][6]
        edited=pixels.generate('하늘을 보라색으로 바꿔줘',s)
        self.assertEqual(edited['frames'][0][0][0],'#8000c0')
        self.assertEqual(edited['frames'][0][12][6],before)
        self.assertEqual(pixels.generate('빨간색')['frames'][0][0][0],'#ff2020')
        with self.assertRaises(pixels.PixelError): pixels.generate('')
        with self.assertRaises(pixels.PixelError): pixels.generate('기차를 그려줘')
        with self.assertRaises(pixels.PixelError): pixels.generate('하늘을 바꿔',s)
    def test_demo_compound_edit_preserves_artwork_and_increases_snow(self):
        original = pixels.generate('눈 내리는 크리스마스 마을을 만들어줘')
        self.assertTrue(any('#16764a' in row for row in original['frames'][0]))
        snapshot = json.loads(json.dumps(original))
        edited = pixels.generate('하늘을 보라색으로 바꾸고 눈을 더 많이 내려줘', original)
        self.assertEqual(original, snapshot)
        self.assertEqual(edited['frames'][0][0][0], '#8000c0')
        self.assertEqual(edited['frames'][0][10][7], original['frames'][0][10][7])
        self.assertGreater(sum(row.count('#ffffff') for row in edited['frames'][0]),
                           sum(row.count('#ffffff') for row in original['frames'][0]))
        self.assertNotEqual(edited['frames'][0], edited['frames'][1])

    def test_repeated_snow_requests_add_visible_flakes(self):
        original = pixels.generate('눈 내리는 마을')
        first = pixels.generate('눈을 더 많이 내려줘', original)
        second = pixels.generate('눈을 더 많이 내려줘', first)
        count = lambda scene: sum(row.count('#ffffff') for row in scene['frames'][0])
        self.assertGreater(count(first), count(original))
        self.assertGreater(count(second), count(first))
        white = {'frames':[pixels.blank('#ffffff')], 'duration':200}
        with self.assertRaises(pixels.PixelError):
            pixels.generate('눈을 더 많이 내려줘', white)
        self.assertEqual(white['frames'][0][0][0], '#ffffff')

    def test_snow_repeats_at_the_wrap_boundary(self):
        scene = pixels.generate('snow')
        frames = scene['frames']
        self.assertEqual(len(frames), 13)
        # Each flake advances one row modulo 13, including the final-to-first transition.
        for i in range(32):
            x = (i * 37 + 7) % pixels.WIDTH
            y = (i * 7) % 13
            for t, frame in enumerate(frames):
                self.assertEqual(frame[(y + t) % 13][x], '#ffffff')

    def test_adding_snow_preserves_imported_animation_length(self):
        frames = [pixels.blank('#101020') for _ in range(3)]
        for index, frame in enumerate(frames):
            frame[15][index] = '#ff0000'
        original = {'frames': frames, 'duration': 300}
        edited = pixels.generate('눈을 더 내려줘', original)
        self.assertEqual((len(edited['frames']), edited['duration']), (3, 300))
        for index, frame in enumerate(edited['frames']):
            self.assertEqual(frame[15][index], '#ff0000')

    def test_sky_edit_preserves_painted_corner(self):
        original = pixels.generate('눈 내리는 마을')
        for frame in original['frames']:
            frame[0][0] = '#123456'
        edited = pixels.generate('하늘을 보라색으로 바꾸고 눈을 더 많이 내려줘', original)
        self.assertEqual(edited['frames'][0][0][0], '#123456')
        self.assertEqual(edited['frames'][0][0][1], '#8000c0')
        self.assertEqual(original['frames'][0][0][1], '#091537')

    def test_modification_never_silently_replaces_existing_scene(self):
        original = pixels.generate('눈 내리는 마을')
        snapshot = json.loads(json.dumps(original))
        result = pixels.generate('눈을 더 내려줘', original)
        self.assertGreater(sum(r.count('#ffffff') for r in result['frames'][0]),
                           sum(r.count('#ffffff') for r in original['frames'][0]))
        for prompt in ('나무를 추가해줘', '눈을 줄여줘', '집을 빨간색으로 바꿔줘'):
            with self.subTest(prompt=prompt):
                with self.assertRaises(pixels.PixelError): pixels.generate(prompt, original)
                self.assertEqual(original, snapshot)

    def test_implicit_sky_edit_preserves_scene_and_new_scene_is_explicit(self):
        original = pixels.generate('눈 내리는 마을')
        original['frames'][0][15][0] = '#123456'
        edited = pixels.generate('하늘을 보라색으로', original)
        self.assertEqual(edited['frames'][0][0][0], '#8000c0')
        self.assertEqual(edited['frames'][0][15][0], '#123456')
        fresh = pixels.generate('보라색 하늘을 만들어줘', original)
        self.assertEqual(fresh['frames'][0][0][0], '#8000c0')
        self.assertNotEqual(fresh['frames'][0][15][0], '#123456')

    def test_image_and_animation_roundtrip(self):
        s=pixels.generate('snow village')
        for fmt in ('png','gif'):
            encoded=pixels.export_image(s,fmt); loaded=pixels.import_image(encoded)
            self.assertEqual(len(loaded['frames']),1 if fmt=='png' else 13)
            self.assertEqual(loaded['frames'][0],s['frames'][0])
            if fmt=='gif':
                im=Image.open(io.BytesIO(base64.b64decode(encoded)))
                self.assertEqual(im.info['loop'],0)
                self.assertEqual(loaded['duration'],200)
        with self.assertRaises(pixels.PixelError): pixels.import_image('not base64')
        with self.assertRaises(pixels.PixelError): pixels.export_image(s,'jpeg')
    def test_import_uses_first_gif_frame_duration(self):
        data = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(
            data, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (2, 2), 'blue')],
            duration=[100, 400], loop=0)
        scene = pixels.import_image(base64.b64encode(data.getvalue()).decode())
        self.assertEqual(len(scene['frames']), 2)
        self.assertEqual(scene['duration'], 100)
        exported = Image.open(io.BytesIO(base64.b64decode(pixels.export_image(scene, 'gif'))))
        self.assertEqual(exported.info['duration'], 100)

    def test_validation(self):
        for s in ({}, {'frames':[]}, {'frames':[pixels.blank()],'duration':0}, {'frames':[[['red']]]}):
            with self.assertRaises(pixels.PixelError): pixels.validate(s)
    def test_provider_contract_and_validation(self):
        s=pixels.generate('snow')
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n): return json.dumps(s).encode()
        with patch('pixels.urllib.request.build_opener') as build:
            call=build.return_value.open
            call.return_value=Response()
            self.assertEqual(pixels.provider_scene('https://example.test/scene','secret','snow',None),s)
            req=call.call_args.args[0]
            self.assertEqual(json.loads(req.data)['width'],144)
            self.assertIsInstance(build.call_args.args[0], pixels._NoProviderRedirect)
        with self.assertRaises(pixels.PixelError): pixels.provider_scene('http://example.test','','x',None)

    def test_provider_redirect_rejected_before_following(self):
        handler = pixels._NoProviderRedirect()
        request = pixels.urllib.request.Request(
            'https://provider.test/scene', data=b'{}',
            headers={'Authorization': 'Bearer synthetic-token'})
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                with self.assertRaises(pixels.urllib.error.HTTPError) as raised:
                    handler.redirect_request(request, None, code, 'redirect', {},
                                             'https://other.test/collect')
                self.assertNotIn('synthetic-token', str(raised.exception))
                raised.exception.close()

    def test_provider_opener_does_not_follow_http_redirect(self):
        requests = []
        class RedirectServer(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/collect')
                self.end_headers()
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:%d/scene' % server.server_port
            request = pixels.urllib.request.Request(url, data=b'{}',
                headers={'Authorization': 'Bearer synthetic-token'})
            opener = pixels.urllib.request.build_opener(pixels._NoProviderRedirect())
            with self.assertRaises(pixels.urllib.error.HTTPError) as raised:
                opener.open(request, timeout=3)
            raised.exception.close()
            self.assertEqual(requests, ['/scene'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__=='__main__': unittest.main()
