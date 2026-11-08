
#include "WarehouseMapLoader.h"

#include "Dom/JsonObject.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "AGVPathActor.h"

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

        // 1. 시작 노드 및 도착 노드 ID 읽기
        if (!EdgeObject->TryGetStringField(TEXT("from"), NewEdge.From) ||
            !EdgeObject->TryGetStringField(TEXT("to"), NewEdge.To))
        {
            UE_LOG(LogTemp, Error, TEXT("Invalid edge fields"));
            return false;
        }

        // 2. 경로 종류 읽기
        if (!EdgeObject->TryGetStringField(TEXT("type"), NewEdge.Type))
        {
            UE_LOG(LogTemp, Error,
                TEXT("Missing edge type: %s -> %s"),
                *NewEdge.From, *NewEdge.To);
            return false;
        }

        // 3. 곡선 중간 좌표 배열 읽기
        const TArray<TSharedPtr<FJsonValue>>* JsonPoints = nullptr;

        if (!EdgeObject->TryGetArrayField(TEXT("points"), JsonPoints))
        {
            UE_LOG(LogTemp, Error,
                TEXT("Missing points array: %s -> %s"),
                *NewEdge.From, *NewEdge.To);
            return false;
        }

        for (const TSharedPtr<FJsonValue>& PointValue : *JsonPoints)
        {
            TSharedPtr<FJsonObject> PointObject = PointValue->AsObject();

            if (!PointObject.IsValid())
            {
                UE_LOG(LogTemp, Error, TEXT("Invalid edge point"));
                return false;
            }

            double X = 0.0;
            double Y = 0.0;

            if (!PointObject->TryGetNumberField(TEXT("x"), X) ||
                !PointObject->TryGetNumberField(TEXT("y"), Y))
            {
                UE_LOG(LogTemp, Error,
                    TEXT("Invalid point coordinates"));
                return false;
            }

            // JSON meter -> Unreal centimeter
            FVector PointPosition(X * 100.0, Y * 100.0, 0.0);

            NewEdge.Points.Add(PointPosition);
        }

        // 4. 완성된 Edge 저장
        ParsedEdges.Add(MoveTemp(NewEdge));

        const FWarehouseEdge& AddedEdge = ParsedEdges.Last();

        UE_LOG(LogTemp, Warning,
            TEXT("Edge: %s -> %s | Type: %s | Points: %d"),
            *AddedEdge.From,
            *AddedEdge.To,
            *AddedEdge.Type,
            AddedEdge.Points.Num());
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

void AWarehouseMapLoader::SpawnPaths()
{
    if (!GetWorld())
    {
        return;
    }

    // 이전에 생성한 경로 제거
    for (AAGVPathActor* Path : SpawnedPaths)
    {
        if (IsValid(Path))
        {
            Path->Destroy();
        }
    }

    SpawnedPaths.Empty();

    // 모든 Edge 순회
    for (const FWarehouseEdge& Edge : Edges)
    {
        const FWarehouseNode* StartNode = Nodes.FindByPredicate(
            [&Edge](const FWarehouseNode& Node)
            {
                return Node.Id == Edge.From;
            });

        const FWarehouseNode* EndNode = Nodes.FindByPredicate(
            [&Edge](const FWarehouseNode& Node)
            {
                return Node.Id == Edge.To;
            });

        if (!StartNode || !EndNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("Path node not found: %s -> %s"),
                *Edge.From, *Edge.To);
            continue;
        }

        TArray<FVector> PathPoints;

        // 시작점
        PathPoints.Add(StartNode->Position);

        // 중간 경유점
        PathPoints.Append(Edge.Points);

        // 끝점
        PathPoints.Add(EndNode->Position);

        // 바닥과 겹치지 않도록 약간 위로 이동
        for (FVector& Point : PathPoints)
        {
            Point.Z += 2.0f;
        }

        AAGVPathActor* PathActor =
            GetWorld()->SpawnActor<AAGVPathActor>(
                AAGVPathActor::StaticClass(),
                FVector::ZeroVector,
                FRotator::ZeroRotator);

        if (PathActor)
        {
            PathActor->BuildPath(PathPoints);
            SpawnedPaths.Add(PathActor);
        }
    }

    UE_LOG(LogTemp, Warning,
        TEXT("Spawned %d AGV paths"),
        SpawnedPaths.Num());
}

bool AWarehouseMapLoader::FindShortestRoute(
    const FString& StartId,
    const FString& EndId,
    TArray<int32>& OutEdgeIndices,
    TArray<bool>& OutReverseFlags) const
{
    OutEdgeIndices.Reset();
    OutReverseFlags.Reset();

    const int32 NodeCount = Nodes.Num();

    const int32 StartIndex = Nodes.IndexOfByPredicate(
        [&StartId](const FWarehouseNode& Node)
        {
            return Node.Id == StartId;
        });

    const int32 EndIndex = Nodes.IndexOfByPredicate(
        [&EndId](const FWarehouseNode& Node)
        {
            return Node.Id == EndId;
        });

    if (StartIndex == INDEX_NONE || EndIndex == INDEX_NONE)
    {
        UE_LOG(LogTemp, Error,
            TEXT("Route node not found: %s -> %s"),
            *StartId, *EndId);
        return false;
    }

    if (StartIndex == EndIndex)
    {
        return true;
    }

    TArray<double> Distances;
    TArray<bool> Visited;
    TArray<int32> PreviousNodes;
    TArray<int32> PreviousEdges;
    TArray<bool> PreviousReverse;

    Distances.Init(TNumericLimits<double>::Max(), NodeCount);
    Visited.Init(false, NodeCount);
    PreviousNodes.Init(INDEX_NONE, NodeCount);
    PreviousEdges.Init(INDEX_NONE, NodeCount);
    PreviousReverse.Init(false, NodeCount);

    Distances[StartIndex] = 0.0;

    for (int32 Iteration = 0; Iteration < NodeCount; ++Iteration)
    {
        int32 Current = INDEX_NONE;
        double BestDistance = TNumericLimits<double>::Max();

        for (int32 i = 0; i < NodeCount; ++i)
        {
            if (!Visited[i] && Distances[i] < BestDistance)
            {
                BestDistance = Distances[i];
                Current = i;
            }
        }

        if (Current == INDEX_NONE || Current == EndIndex)
        {
            break;
        }

        Visited[Current] = true;

        for (int32 EdgeIndex = 0; EdgeIndex < Edges.Num(); ++EdgeIndex)
        {
            const FWarehouseEdge& Edge = Edges[EdgeIndex];

            const bool bForward = Edge.From == Nodes[Current].Id;
            const bool bReverse = Edge.To == Nodes[Current].Id;

            if (!bForward && !bReverse)
            {
                continue;
            }

            const FString& NeighborId =
                bForward ? Edge.To : Edge.From;

            const int32 Neighbor = Nodes.IndexOfByPredicate(
                [&NeighborId](const FWarehouseNode& Node)
                {
                    return Node.Id == NeighborId;
                });

            if (Neighbor == INDEX_NONE || Visited[Neighbor])
            {
                continue;
            }

            // Edge의 중간 좌표까지 포함해 실제 경로 길이 계산
            double EdgeLength = 0.0;
            FVector PreviousPosition = Nodes[Current].Position;

            if (bForward)
            {
                for (const FVector& Point : Edge.Points)
                {
                    EdgeLength += FVector::Dist2D(
                        PreviousPosition, Point);
                    PreviousPosition = Point;
                }
            }
            else
            {
                for (int32 p = Edge.Points.Num() - 1; p >= 0; --p)
                {
                    EdgeLength += FVector::Dist2D(
                        PreviousPosition, Edge.Points[p]);
                    PreviousPosition = Edge.Points[p];
                }
            }

            EdgeLength += FVector::Dist2D(
                PreviousPosition, Nodes[Neighbor].Position);

            const double NewDistance =
                Distances[Current] + EdgeLength;

            if (NewDistance < Distances[Neighbor])
            {
                Distances[Neighbor] = NewDistance;
                PreviousNodes[Neighbor] = Current;
                PreviousEdges[Neighbor] = EdgeIndex;
                PreviousReverse[Neighbor] = bReverse;
            }
        }
    }

    if (PreviousEdges[EndIndex] == INDEX_NONE)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("No route found: %s -> %s"),
            *StartId, *EndId);
        return false;
    }

    int32 Current = EndIndex;

    while (Current != StartIndex)
    {
        if (Current == INDEX_NONE ||
            PreviousEdges[Current] == INDEX_NONE)
        {
            OutEdgeIndices.Reset();
            OutReverseFlags.Reset();
            return false;
        }

        OutEdgeIndices.Insert(PreviousEdges[Current], 0);
        OutReverseFlags.Insert(PreviousReverse[Current], 0);

        Current = PreviousNodes[Current];
    }

    UE_LOG(LogTemp, Warning,
        TEXT("Shortest route found: %s -> %s | Edges: %d"),
        *StartId, *EndId, OutEdgeIndices.Num());

    return true;
}

void AWarehouseMapLoader::GenerateMapInEditor()
{
    if (!LoadMap())
    {
        UE_LOG(LogTemp, Error,
            TEXT("[Warehouse] Failed to load map in editor"));
        return;
    }

    SpawnPaths();

    UE_LOG(LogTemp, Warning,
        TEXT("[Warehouse] Map generated in editor"));
}
