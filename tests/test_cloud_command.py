import base64, importlib.util, os, pathlib, subprocess, unittest

spec = importlib.util.spec_from_file_location(
    "cloud", pathlib.Path(__file__).parent.parent / "scripts" / "cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)

class CloudCommandTests(unittest.TestCase):
    def test_large_reviewed_program_executes_identically_within_transport_limit(self):
        source = "# fixed release code\n" * 3000 + "print('verified release program')\n"
        command = cloud.python_command(source)
        self.assertLess(len(base64.b64encode(command.encode())), 16 * 1024)
        result = subprocess.run(["bash", "-c", command], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, check=True)
        self.assertEqual(result.stdout, "verified release program\n")

    def test_oversized_incompressible_command_is_rejected_before_dispatch(self):
        source = "#" + base64.b64encode(os.urandom(40000)).decode()
        with self.assertRaisesRegex(RuntimeError, "exceeds ECS content limit"):
            cloud.python_command(source)
