
#include "WarehouseMapLoader.h"

#include "Dom/JsonObject.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

AWarehouseMapLoader::AWarehouseMapLoader()
{
    PrimaryActorTick.bCanEverTick = false;
}

void AWarehouseMapLoader::BeginPlay()
{
    Super::BeginPlay();
}

bool AWarehouseMapLoader::LoadMap()
{
    const FString FilePath =
        FPaths::ProjectContentDir()
        / TEXT("Data/warehouse_map.json");

    FString JsonString;

    if (!FFileHelper::LoadFileToString(
        JsonString, *FilePath))
    {
        UE_LOG(LogTemp, Error,
            TEXT("JSON file not found: %s"), *FilePath);

        return false;
    }

    TSharedPtr<FJsonObject> JsonObject;
    TSharedRef<TJsonReader<>> Reader =
        TJsonReaderFactory<>::Create(JsonString);

    if (!FJsonSerializer::Deserialize(
        Reader, JsonObject) || !JsonObject.IsValid())
    {
        UE_LOG(LogTemp, Error,
            TEXT("JSON parsing failed"));

        return false;
    }

    // JSON 배열 포인터 (멤버변수 Nodes와 구분)
    const TArray<TSharedPtr<FJsonValue>>* JsonNodes = nullptr;

    if (!JsonObject->TryGetArrayField(
        TEXT("nodes"), JsonNodes))
    {
        UE_LOG(LogTemp, Error,
            TEXT("nodes array not found"));

        return false;
    }

    // 기존 노드 데이터 초기화
    TArray<FWarehouseNode> ParsedNodes;

    for (const TSharedPtr<FJsonValue>& Value : *JsonNodes)
    {
        TSharedPtr<FJsonObject> Node = Value->AsObject();

        if (!Node.IsValid())
        {
            UE_LOG(LogTemp, Error,
                TEXT("Invalid node object"));
            return false;
        }

        FString Id;
        double X = 0.0;
        double Y = 0.0;

        if (!Node->TryGetStringField(TEXT("id"), Id)
            || !Node->TryGetNumberField(TEXT("x"), X)
            || !Node->TryGetNumberField(TEXT("y"), Y))
        {
            UE_LOG(LogTemp, Error,
                TEXT("Invalid node fields"));
            return false;
        }

        FWarehouseNode NewNode;

        NewNode.Id = Id;

        // JSON 좌표(m) -> Unreal 좌표(cm)
        NewNode.Position = FVector(
            X * 100.0,
            Y * 100.0,
            0.0
        );

        ParsedNodes.Add(NewNode);

        UE_LOG(LogTemp, Warning,
            TEXT("Node: %s | X: %.2f | Y: %.2f"),
            *Id, X, Y);
    }

    // JSON에서 edges 배열 읽기
    const TArray<TSharedPtr<FJsonValue>>* JsonEdges = nullptr;

    if (!JsonObject->TryGetArrayField(TEXT("edges"), JsonEdges))
    {
        UE_LOG(LogTemp, Error, TEXT("edges array not found"));
        return false;
    }

    TArray<FWarehouseEdge> ParsedEdges;

    for (const TSharedPtr<FJsonValue>& Value : *JsonEdges)
    {
        TSharedPtr<FJsonObject> EdgeObject = Value->AsObject();

        if (!EdgeObject.IsValid())
        {
            UE_LOG(LogTemp, Error, TEXT("Invalid edge object"));
            return false;
        }

        FWarehouseEdge NewEdge;

        if (!EdgeObject->TryGetStringField(TEXT("from"), NewEdge.From) ||
            !EdgeObject->TryGetStringField(TEXT("to"), NewEdge.To))
        {
            UE_LOG(LogTemp, Error, TEXT("Invalid edge fields"));
            return false;
        }

        ParsedEdges.Add(NewEdge);

        UE_LOG(LogTemp, Warning,
            TEXT("Edge: %s -> %s"),
            *NewEdge.From,
            *NewEdge.To);
    }

    // 파싱 완료 후 저장
    Nodes = MoveTemp(ParsedNodes);
    Edges = MoveTemp(ParsedEdges);

    UE_LOG(LogTemp, Warning,
        TEXT("Warehouse JSON loaded successfully: %d nodes, %d edges"),
        Nodes.Num(),
        Edges.Num());

    return true;
}
