from .main import create_app
'''
build the app so uvicorn can find it (uvicorn app.asgi:app)
'''
app = create_app()
