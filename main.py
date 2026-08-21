import webview
import os
import sys
import json
import zipfile
import base64

def get_base_path():
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

class Api:
    def __init__(self):
        self.window = None

    def save_scene(self, config_json, idle_b64, speaking_b64, idle_blink_b64, speaking_blink_b64):
        file_path = self.window.create_file_dialog(webview.SAVE_DIALOG, directory='', save_filename='model.pngtuber')
        if not file_path:
            return False
        
        file_path = file_path[0]
        
        try:
            with zipfile.ZipFile(file_path, 'w') as zf:
                zf.writestr('config.json', config_json)
                if idle_b64:
                    zf.writestr('idle.png', base64.b64decode(idle_b64.split(',')[1]))
                if speaking_b64:
                    zf.writestr('speaking.png', base64.b64decode(speaking_b64.split(',')[1]))
                if idle_blink_b64:
                    zf.writestr('idle_blink.png', base64.b64decode(idle_blink_b64.split(',')[1]))
                if speaking_blink_b64:
                    zf.writestr('speaking_blink.png', base64.b64decode(speaking_blink_b64.split(',')[1]))
            return True
        except Exception as e:
            print("Error saving scene:", e)
            return False

    def load_scene(self):
        file_path = self.window.create_file_dialog(webview.OPEN_DIALOG, file_types=('PNGTuber Scene (*.pngtuber)', 'All files (*.*)'))
        if not file_path:
            return None
            
        file_path = file_path[0]
        
        result = {}
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                if 'config.json' in zf.namelist():
                    result['config'] = json.loads(zf.read('config.json').decode('utf-8'))
                
                for img_name in ['idle.png', 'speaking.png', 'idle_blink.png', 'speaking_blink.png']:
                    if img_name in zf.namelist():
                        mime = 'image/png'
                        result[img_name.split('.')[0]] = f"data:{mime};base64," + base64.b64encode(zf.read(img_name)).decode('utf-8')
            return result
        except Exception as e:
            print("Error loading scene:", e)
            return None

if __name__ == '__main__':
    index_path = os.path.join(get_base_path(), 'index.html')
    
    api = Api()
    
    window = webview.create_window(
        'Оптимизированный PNGTuber',
        url=index_path,
        js_api=api,
        width=500,
        height=600,
        resizable=True,
        transparent=True,
        frameless=False,
        easy_drag=True
    )
    
    api.window = window
    
    webview.start(http_server=True, debug=False)
