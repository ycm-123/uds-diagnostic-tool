import can
import isotp
import time
import yaml
import sys
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.primitives.ciphers import algorithms
SECRET_KEY = b'\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0A\x0B\x0C\x0D\x0E\x0F\x10'

def compute_key(seed):
    """用 AES-CMAC 对种子计算密钥"""
    c = CMAC(algorithms.AES(SECRET_KEY))
    c.update(seed)
    return c.finalize()
def send_and_wait(stack, payload, timeout=2.0):
    """发送请求并等待响应"""
    print(f"[客户端] 发送: {payload.hex(' ').upper()}")
    stack.send(payload)

    end = time.time() + timeout
    while time.time() < end:
        if stack.available():
            resp = stack.recv()
            print(f"[客户端] 收到: {resp.hex(' ').upper()}")
            return resp
        time.sleep(0.05)
    print("[客户端] 超时")
    return None

def run_test_cases(stack, case_file='test_cases.yaml'):
    """从 YAML 文件读取测试用例并执行"""
    with open(case_file, 'r', encoding='utf-8') as f:
        cases = yaml.safe_load(f)['test_cases']
    print(f"[DEBUG]读到{len(cases)}条用例")
    print("[客户端] 测试前重置：回默认会话")
    send_and_wait(stack, bytes([0x10, 0x01]))

    passed = 0
    failed = 0

    for case in cases:
        name = case['name']
        print(f"\n=== {name} ===")

        try:
            # ===== 动态用例：正确密钥解锁 =====
            if case.get('dynamic') == 'unlock':
                resp = send_and_wait(stack, bytes([0x27, 0x01]))
                assert resp is not None, "请求种子超时"
                assert resp[0] == 0x67, f"请求种子失败，实际 0x{resp[0]:02X}"
                seed = resp[2:]
                print(f"[客户端] 收到种子: {' '.join(f'{b:02X}' for b in seed)}")

                key = compute_key(seed)
                print(f"[客户端] 计算密钥: {' '.join(f'{b:02X}' for b in key)}")

                resp = send_and_wait(stack, bytes([0x27, 0x02]) + key)
                assert resp is not None, "发密钥超时"
                assert resp[0] == 0x67, f"解锁失败，实际 0x{resp[0]:02X}"
            elif case.get('dynamic') == 'write_vin':
                # 进扩展会话
                send_and_wait(stack, bytes([0x10, 0x03]))
                # 解锁
                resp = send_and_wait(stack, bytes([0x27, 0x01]))
                seed = resp[2:]
                key = compute_key(seed)
                send_and_wait(stack, bytes([0x27, 0x02]) + key)
                # 写 VIN
                new_vin = b"TESTVIN1234567890"
                resp = send_and_wait(stack, bytes([0x2E, 0xF1, 0x90]) + new_vin)
                assert resp is not None, "写 VIN 超时"
                assert resp[0] == 0x6E, f"写 VIN 失败，实际 0x{resp[0]:02X}"
                # 读回验证
                resp = send_and_wait(stack, bytes([0x22, 0xF1, 0x90]))
                assert resp[3:] == new_vin, "读回的 VIN 和写入的不一致"
                print(f"[PASS] {case['name']}")
                passed += 1
                continue
            elif case.get('dynamic') == 'write_vin_no_unlock':
                # 先回默认会话，确保上锁
                send_and_wait(stack, bytes([0x10, 0x01]))
                # 先进扩展会话
                send_and_wait(stack, bytes([0x10, 0x03]))
                # 不请求种子、不解锁，直接写
                new_vin = b"TESTVIN1234567890"
                resp = send_and_wait(stack, bytes([0x2E, 0xF1, 0x90]) + new_vin)
                assert resp is not None, "写 VIN 超时"
                assert resp[0] == 0x7F, f"期望否定响应，实际 0x{resp[0]:02X}"
                assert resp[2] == 0x33, f"期望 NRC=0x33，实际 0x{resp[2]:02X}"
            else:
                # ===== 普通用例 =====
                req = bytes.fromhex(case['request'].replace(' ', ''))
                resp = send_and_wait(stack, req)

                assert resp is not None, "超时，没收到响应"

                if 'nrc' in case:
                    expected_nrc = int(case['nrc'], 16)
                    assert resp[0] == 0x7F, f"期望否定响应，实际 0x{resp[0]:02X}"
                    assert resp[2] == expected_nrc, \
                        f"期望 NRC=0x{expected_nrc:02X}，实际 0x{resp[2]:02X}"

                if 'expected_response' in case:
                    expected = bytes.fromhex(case['expected_response'].replace(' ', ''))
                    assert resp == expected, \
                        f"期望 {case['expected_response']}，实际 {resp.hex(' ').upper()}"

                if 'expected_prefix' in case:
                    prefix = bytes.fromhex(case['expected_prefix'].replace(' ', ''))
                    assert resp[:len(prefix)] == prefix, \
                        f"前缀期望 {case['expected_prefix']}，实际 {resp.hex(' ').upper()}"

                if 'expected_length' in case:
                    assert len(resp) == case['expected_length'], \
                        f"期望长度 {case['expected_length']}，实际 {len(resp)}"

            print(f"[PASS] {name}")
            passed += 1

        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print(f"\n=== 测试完成: {passed} 通过, {failed} 失败 ===")
    return failed == 0

def run_normal_flow(stack):
    """正常诊断流程"""
    # 1. 进扩展会话
    resp = send_and_wait(stack, bytes([0x10, 0x03]))
    if not resp:
        print("[客户端] 超时，中止诊断")
        return
    if resp[0] == 0x50:
        print("[客户端] 会话切换成功")
    elif resp[0] == 0x7F:
        print(f"[客户端] 否定响应，NRC = 0x{resp[2]:02X}，中止诊断")
        return
    else:
        print(f"[客户端] 异常响应: 0x{resp[0]:02X}，中止诊断")
        return

    # 2. 请求种子
    resp = send_and_wait(stack, bytes([0x27, 0x01]))
    if not resp or resp[0] != 0x67:
        print("[客户端] 请求种子失败，中止")
        return
    seed = resp[2:]
    print(f"[客户端] 收到种子: {' '.join(f'{b:02X}' for b in seed)}")

    # 3. 用 AES-CMAC 计算密钥并发出去
    key = compute_key(seed)
    print(f"[客户端] 计算密钥: {' '.join(f'{b:02X}' for b in key)}")
    resp = send_and_wait(stack, bytes([0x27, 0x02]) + key)
    if not resp or resp[0] != 0x67:
        print("[客户端] 解锁失败，中止")
        return
    print("[客户端] 解锁成功")

    # 4. 写 VIN（解锁后）
    new_vin = b"TESTVIN1234567890"  # 17 字节
    resp = send_and_wait(stack, bytes([0x2E, 0xF1, 0x90]) + new_vin)
    if not resp or resp[0] != 0x6E:
        print("[客户端] 写 VIN 失败，中止")
        return
    print(f"[客户端] 写 VIN 成功")

    # 5. 读回 VIN 验证
    resp = send_and_wait(stack, bytes([0x22, 0xF1, 0x90]))
    if resp and resp[0] == 0x62:
        print(f"[客户端] 读回 VIN = {resp[3:].decode('ascii', errors='replace')}")

    # 6. 心跳
    send_and_wait(stack, bytes([0x3E, 0x80]))

    # 7. 回默认会话
    send_and_wait(stack, bytes([0x10, 0x01]))
def main():
    print(f"[DEBUG] sys.argv = {sys.argv}")
    bus = can.Bus(interface='udp_multicast', channel='224.0.0.1', port=23456)
    addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=0x7E0, rxid=0x7E8)
    stack = isotp.CanStack(bus=bus, address=addr, params={'blocksize': 0, 'stmin': 0})
    stack.start()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'test':
            run_test_cases(stack)
        else:
            run_normal_flow(stack)
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        stack.stop()
        bus.shutdown()

if __name__ == '__main__':
    main()