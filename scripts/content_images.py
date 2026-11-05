"""Image occurrences in controlled-template order (not a unique asset inventory)."""


def iter_content_images(content):
    """Yield ``(JSON slot path, image dict)`` in actual rendering order.

    D/F use images *instead of* image. A uses image/extra_image/final_card.
    Undeclared-track inputs visit all supported slots in canonical order.
    A reused file remains a separate occurrence so count/order checks detect it.
    """
    track = content.get('content_track')
    if track == 'F' and content.get('layout_profile') == 'solar_editorial':
        sections = content.get('sections', [])
        if sections and sections[0].get('image'):
            yield 'sections[0].image', sections[0]['image']
        for index, section in enumerate(sections):
            if index != 0 and section.get('image'):
                yield f'sections[{index}].image', section['image']
            for i, image in enumerate(section.get('images') or []):
                yield f'sections[{index}].images[{i}]', image
        return
    if track in (None, 'A', 'D', 'F') and content.get('lead_image'):
        yield 'lead_image', content['lead_image']
    for index, section in enumerate(content.get('sections', [])):
        base = f'sections[{index}]'
        if track in ('D', 'F'):
            if section.get('images'):
                for i, image in enumerate(section['images']):
                    yield f'{base}.images[{i}]', image
            elif section.get('image'):
                yield f'{base}.image', section['image']
        else:
            slots = {'A': ('image', 'extra_image', 'final_card'),
                     'C': ('gif_or_image',), 'S': ('image',)}.get(track, ())
            if track is None:
                slots = ('image', 'images', 'extra_image', 'final_card', 'gif_or_image')
            for slot in slots:
                if slot == 'images':
                    for i, image in enumerate(section.get(slot) or []):
                        yield f'{base}.images[{i}]', image
                elif section.get(slot):
                    yield f'{base}.{slot}', section[slot]
