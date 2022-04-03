import pytest  # noqa


from pyconnect import parse_VCard21


def test_basic_test():
    with open('tests/vcard_example_1.txt', 'r') as f:
        data = f.read()
    vcard = parse_VCard21(data)
    expected_result = {
        'fn': 'Yannick Albert',
        'tel': [
            {
                'meta': {'type': 'cell', 'type1': 'voice'},
                'value': ['+49 1525 1333833'],
            }
        ],
    }

    assert vcard == expected_result

    with open('tests/vcard_example_2.txt', 'r') as f:
        data = f.read()
    vcard = parse_VCard21(data)
    # vcard z bardziej fake danymi
    expected_result = {
        'fn': 'Konrad Nowak',
        'tel': [
            {'meta': {'type': 'cell'}, 'value': ['12 345 65 6']},
            {'meta': {'type': 'home'}, 'value': ['12 4 5689']},
        ],
        'photo': [
            {
                'meta': {'ENCODING': 'BASE64', 'type1': 'jpeg'},
            }
        ],
        'x-kdeconnect-id-dev-1ee7de01-4d38-4efe-9d91-88de3f4a4aea': '3176r15-3D45434B292F434555293D',  # noqa
        'x-kdeconnect-timestamp': '1648144054548',
    }
    assert vcard['photo'][0]['value'][0].startswith('/9j/4AAQSkZJRgABAQAAAQAB')
    assert vcard['photo'][0]['value'][0].endswith('s/Zd3QYI701rUqpwPyFZ2Hc//Z')
    del vcard['photo'][0]['value']
    assert vcard == expected_result


def test_encodings():
    data = '''BEGIN:VCARD
VERSION:2.1
fn;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=c4=84=c4=85=c4=86=c4=87=c4=98=c4=99=c5=81=c5=82=c3=93=c3=b3=c5=83=c5=84=c5=9a=c5=9b=c5=bb=c5=bc=c5=b9=c5=ba
END:VCARD
'''  # noqa

    vcard = parse_VCard21(data)
    expected_result = {'fn': 'ĄąĆćĘęŁłÓóŃńŚśŻżŹź', 'tel': []}
    assert vcard == expected_result
