def vfs_write(file_path, append, data, data_hash, data_hash_algo):
    from binascii import crc32, a2b_base64
    import os

    if data_hash_algo != "crc32":
        raise NotImplementedError("Only CRC32 is supported for now.")

    actual_hash = crc32(data)
    if actual_hash != hash:
        raise ValueError(f"Hash mismatch, expected {hex(hash)}, got {hex(actual_hash)}")

    mode = "a" if append else "w"

    file_path = file_path.rstrip("/")
    path_parts = file_path.split("/")
    file_name = path_parts[-1]

    root_dir = os.getcwd()
    file_root = path_parts[0]
    if file_root != root_dir:
        raise ValueError(f"Invalid file path: {file_path}. Root directory must be {root_dir}.")

    path_parts = path_parts[1:] # Skip the root directory
    for part in path_parts[:-1]:
        if part not in os.listdir():
            os.mkdir(part)
        os.chdir(part)

    with open(file_name, mode) as f:
        f.write(a2b_base64(data))

    os.chdir(root_dir)
