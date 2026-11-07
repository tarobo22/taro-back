"""Create resume-ready QR files without a remote QR service."""
import argparse
from pathlib import Path

import qrcode
import qrcode.image.svg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.url.startswith('https://'):
        raise SystemExit('Use the verified public HTTPS URL.')
    args.output.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q, box_size=16, border=4)
    qr.add_data(args.url)
    qr.make(fit=True)
    qr.make_image(fill_color='#214F41', back_color='white').save(args.output / 'yeoun-demo-qr.png')
    qr.make_image(image_factory=qrcode.image.svg.SvgPathFillImage).save(args.output / 'yeoun-demo-qr.svg')
    (args.output / 'yeoun-demo-url.txt').write_text(args.url + '\n', encoding='utf-8')
    print(f'QR created: {args.url}')


if __name__ == '__main__':
    main()
