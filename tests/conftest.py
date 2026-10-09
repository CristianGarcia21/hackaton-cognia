def pytest_addoption(parser):
    parser.addoption("--red", action="store_true", default=False, help="corre tests que usan APIs reales")
